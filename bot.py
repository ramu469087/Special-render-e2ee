# bot_webui.py - FULLY FIXED for Render 512MB RAM
# Desktop User-Agent + Hard Kill + Auto Restart

import os
import sys
import time
import json
import random
import sqlite3
import threading
import gc
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass
from collections import deque
from functools import wraps

from flask import Flask, render_template_string, request, jsonify, session, redirect, url_for
from cryptography.fernet import Fernet
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# ==================== CONFIGURATION ====================
MAX_TASKS = 1
PORT = int(os.environ.get("PORT", 5000))
BROWSER_RESTART_HOURS = 2  # Har 2 hours restart (memory leak se bachne ke liye)

DB_PATH = Path(__file__).parent / 'bot_data.db'
ENCRYPTION_KEY_FILE = Path(__file__).parent / '.encryption_key'

task_logs = {}

def log_message(task_id: str, msg: str):
    timestamp = time.strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {msg}"
    if task_id not in task_logs:
        task_logs[task_id] = deque(maxlen=100)
    task_logs[task_id].append(formatted_msg)
    print(formatted_msg)

# ==================== HARD KILL ====================
def hard_kill_chromium(task_id: str = ""):
    """Force kill all chromium processes"""
    try:
        subprocess.run(['pkill', '-9', '-f', 'chromium'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run(['pkill', '-9', '-f', 'chromedriver'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run(['rm', '-rf', '/dev/shm/.org.chromium*'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        if task_id:
            log_message(task_id, "🔪 Hard kill completed")
    except:
        pass

# ==================== ENCRYPTION ====================
def get_encryption_key():
    if ENCRYPTION_KEY_FILE.exists():
        with open(ENCRYPTION_KEY_FILE, 'rb') as f:
            return f.read()
    else:
        key = Fernet.generate_key()
        with open(ENCRYPTION_KEY_FILE, 'wb') as f:
            f.write(key)
        return key

ENCRYPTION_KEY = get_encryption_key()
cipher_suite = Fernet(ENCRYPTION_KEY)

def encrypt_data(data):
    if not data:
        return None
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data):
    if not encrypted_data:
        return ""
    try:
        return cipher_suite.decrypt(encrypted_data.encode()).decode()
    except:
        return ""

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            cookies_encrypted TEXT,
            chat_id TEXT,
            name_prefix TEXT,
            messages TEXT,
            delay INTEGER DEFAULT 60,
            status TEXT DEFAULT 'stopped',
            messages_sent INTEGER DEFAULT 0,
            rotation_index INTEGER DEFAULT 0,
            start_time TIMESTAMP,
            last_active TIMESTAMP,
            last_restart TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    import hashlib
    cursor.execute('SELECT * FROM users WHERE username = "admin"')
    if not cursor.fetchone():
        password_hash = hashlib.sha256("admin123".encode()).hexdigest()
        cursor.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', 
                      ('admin', password_hash))
    
    conn.commit()
    conn.close()

init_db()

@dataclass
class Task:
    task_id: str
    username: str
    cookies: List[str]
    chat_id: str
    name_prefix: str
    messages: List[str]
    delay: int
    status: str
    messages_sent: int
    start_time: Optional[datetime]
    last_active: Optional[datetime]
    last_restart: Optional[datetime]
    running: bool = False
    stop_flag: bool = False
    rotation_index: int = 0
    
    def get_uptime(self):
        if not self.start_time:
            return "00:00:00"
        delta = datetime.now() - self.start_time
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        seconds = delta.seconds % 60
        if days > 0:
            return f"{days}d {hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

class TaskManager:
    def __init__(self):
        self.tasks: Dict[str, Task] = {}
        self.task_threads: Dict[str, threading.Thread] = {}
        self.load_tasks_from_db()
    
    def load_tasks_from_db(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM tasks')
        for row in cursor.fetchall():
            try:
                cookies = json.loads(decrypt_data(row[2])) if row[2] else []
                messages = json.loads(decrypt_data(row[5])) if row[5] else []
                
                task = Task(
                    task_id=row[0],
                    username=row[1],
                    cookies=cookies,
                    chat_id=row[3] or "",
                    name_prefix=row[4] or "",
                    messages=messages,
                    delay=row[6] or 60,
                    status=row[7] or "stopped",
                    messages_sent=row[8] or 0,
                    start_time=datetime.fromisoformat(row[10]) if row[10] else None,
                    last_active=datetime.fromisoformat(row[11]) if row[11] else None,
                    last_restart=datetime.fromisoformat(row[12]) if row[12] else None,
                    rotation_index=row[9] or 0
                )
                self.tasks[task.task_id] = task
                if task.status == "running":
                    self.start_task(task.task_id)
            except Exception as e:
                print(f"Error loading task: {e}")
        conn.close()
    
    def save_task(self, task: Task):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO tasks 
            (task_id, username, cookies_encrypted, chat_id, name_prefix, messages, 
             delay, status, messages_sent, rotation_index, start_time, last_active, last_restart)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task.task_id,
            task.username,
            encrypt_data(json.dumps(task.cookies)),
            task.chat_id,
            task.name_prefix,
            encrypt_data(json.dumps(task.messages)),
            task.delay,
            task.status,
            task.messages_sent,
            task.rotation_index,
            task.start_time.isoformat() if task.start_time else None,
            task.last_active.isoformat() if task.last_active else None,
            task.last_restart.isoformat() if task.last_restart else None
        ))
        conn.commit()
        conn.close()
    
    def delete_task(self, task_id: str):
        if task_id in self.tasks:
            self.stop_task(task_id)
            del self.tasks[task_id]
            if task_id in task_logs:
                del task_logs[task_id]
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))
            conn.commit()
            conn.close()
            return True
        return False
    
    def start_task(self, task_id: str):
        if task_id not in self.tasks:
            return False
        task = self.tasks[task_id]
        if task.status == "running":
            return False
        if len([t for t in self.tasks.values() if t.status == "running"]) >= MAX_TASKS:
            log_message(task_id, f"Max tasks limit reached ({MAX_TASKS})")
            return False
        task.status = "running"
        task.stop_flag = False
        if not task.start_time:
            task.start_time = datetime.now()
        if not task.last_restart:
            task.last_restart = datetime.now()
        task.last_active = datetime.now()
        self.save_task(task)
        
        thread = threading.Thread(target=self._run_task, args=(task_id,), daemon=True)
        thread.start()
        self.task_threads[task_id] = thread
        return True
    
    def stop_task(self, task_id: str):
        if task_id not in self.tasks:
            return False
        task = self.tasks[task_id]
        task.stop_flag = True
        task.status = "stopped"
        task.last_active = datetime.now()
        self.save_task(task)
        return True
    
    def _setup_browser(self, task_id: str):
        """DESKTOP User-Agent - Stable Facebook selectors"""
        hard_kill_chromium(task_id)
        
        chrome_options = Options()
        
        # Headless + essential
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-setuid-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        
        # Memory optimization for 512MB
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--disable-plugins')
        chrome_options.add_argument('--blink-settings=imagesEnabled=false')
        chrome_options.add_argument('--max_old_space_size=128')
        chrome_options.add_argument('--js-flags="--max-old-space-size=128"')
        chrome_options.add_argument('--memory-pressure-off')
        
        # CRITICAL: Desktop User-Agent (Mobile band ho chuka hai)
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36')
        
        chrome_options.add_argument('--window-size=1280,720')
        chrome_options.add_argument('--disable-background-timer-throttling')
        chrome_options.add_argument('--disable-backgrounding-occluded-windows')
        chrome_options.add_argument('--disable-renderer-backgrounding')
        chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        
        # Find Chromium
        chromium_paths = ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome', '/usr/bin/chrome']
        for path in chromium_paths:
            if Path(path).exists():
                chrome_options.binary_location = path
                log_message(task_id, f'Found browser at: {path}')
                break
        
        # Find ChromeDriver
        driver_paths = ['/usr/bin/chromedriver', '/usr/local/bin/chromedriver']
        driver_path = None
        for path in driver_paths:
            if Path(path).exists():
                driver_path = path
                log_message(task_id, f'Found ChromeDriver at: {path}')
                break
        
        try:
            if driver_path:
                service = Service(executable_path=driver_path)
                driver = webdriver.Chrome(service=service, options=chrome_options)
            else:
                driver = webdriver.Chrome(options=chrome_options)
            
            driver.set_window_size(1280, 720)
            driver.set_page_load_timeout(30)
            log_message(task_id, '✅ Browser setup successful!')
            return driver
            
        except Exception as error:
            log_message(task_id, f'Browser setup failed: {error}')
            hard_kill_chromium(task_id)
            raise error
    
    def _find_message_input(self, driver, task_id: str, process_id: str):
        """Working selectors for Desktop Facebook"""
        log_message(task_id, f"{process_id}: Finding message input...")
        
        # Wait for composer to load
        time.sleep(2)
        
        # Try to click on chat area first
        try:
            driver.execute_script("""
                const chatArea = document.querySelector('[role="main"]');
                if(chatArea) chatArea.click();
            """)
            time.sleep(1)
        except:
            pass
        
        # Desktop Facebook selectors (only these work)
        selectors = [
            'div[contenteditable="true"][role="textbox"]',
            'div[aria-label="Message"][role="textbox"]',
            'div[aria-label="Aa"][role="textbox"]',
            'div[contenteditable="true"][data-lexical-editor="true"]',
            '[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"]',
            'form div[contenteditable="true"]'
        ]
        
        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    if element.is_displayed() and element.is_enabled():
                        # Check if it's at bottom of page (message input)
                        is_bottom = driver.execute_script("""
                            const rect = arguments[0].getBoundingClientRect();
                            return rect.y > window.innerHeight - 200;
                        """, element)
                        
                        if is_bottom:
                            element.click()
                            time.sleep(0.5)
                            log_message(task_id, f"{process_id}: ✅ Found using: {selector}")
                            return element
            except:
                continue
        
        # Last resort
        try:
            element = driver.find_element(By.CSS_SELECTOR, '[contenteditable="true"]')
            log_message(task_id, f"{process_id}: ✅ Found via fallback")
            return element
        except:
            pass
        
        log_message(task_id, f"{process_id}: ❌ Message input not found!")
        return None
    
    def _send_message(self, driver, message_input, task: Task, task_id: str, process_id: str):
        """Send message with delay 60+ seconds"""
        messages_list = [msg.strip() for msg in task.messages if msg.strip()]
        if not messages_list:
            messages_list = ['Hello!']
        
        msg_idx = task.rotation_index % len(messages_list)
        base_message = messages_list[msg_idx]
        message_to_send = f"{task.name_prefix} {base_message}" if task.name_prefix else base_message
        
        try:
            # Type message
            driver.execute_script("""
                const element = arguments[0];
                const message = arguments[1];
                
                element.scrollIntoView({behavior: 'smooth', block: 'center'});
                element.focus();
                element.click();
                
                if (element.tagName === 'DIV') {
                    element.textContent = message;
                    element.innerHTML = message;
                } else {
                    element.value = message;
                }
                
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
            """, message_input, message_to_send)
            
            time.sleep(1.5)
            
            # Send button or Enter
            sent = driver.execute_script("""
                const sendButtons = document.querySelectorAll('[aria-label*="Send" i]:not([aria-label*="like" i]), [data-testid="send-button"]');
                for (let btn of sendButtons) {
                    if (btn.offsetParent !== null) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            """)
            
            if not sent:
                message_input.send_keys(Keys.ENTER)
                log_message(task_id, f"{process_id}: ✅ Sent via Enter")
            else:
                log_message(task_id, f"{process_id}: ✅ Sent via button")
            
            # Update counters
            task.messages_sent += 1
            task.rotation_index += 1
            task.last_active = datetime.now()
            self.save_task(task)
            
            log_message(task_id, f"{process_id}: Message #{task.messages_sent} sent")
            return True
            
        except Exception as e:
            log_message(task_id, f"{process_id}: Send error: {str(e)[:100]}")
            return False
    
    def _run_task(self, task_id: str):
        """Main task runner with auto restart"""
        task = self.tasks[task_id]
        task.running = True
        process_id = f"TASK-{task_id[-6:]}"
        
        driver = None
        message_input = None
        consecutive_failures = 0
        
        while task.status == "running" and not task.stop_flag:
            try:
                # Check if restart needed (every 2 hours minimum delay)
                needs_restart = False
                if task.last_restart:
                    hours_since = (datetime.now() - task.last_restart).total_seconds() / 3600
                    if hours_since >= BROWSER_RESTART_HOURS:
                        needs_restart = True
                        log_message(task_id, f"{process_id}: {BROWSER_RESTART_HOURS} hours completed, restarting...")
                
                if driver is None or needs_restart:
                    if driver:
                        try:
                            driver.quit()
                        except:
                            pass
                    
                    hard_kill_chromium(task_id)
                    time.sleep(2)
                    
                    # Create new browser
                    for retry in range(3):
                        try:
                            driver = self._setup_browser(task_id)
                            if driver:
                                break
                        except Exception as e:
                            log_message(task_id, f"{process_id}: Retry {retry+1}/3: {str(e)[:50]}")
                            hard_kill_chromium(task_id)
                            time.sleep(5)
                    
                    if not driver:
                        log_message(task_id, f"{process_id}: Failed to create browser!")
                        time.sleep(30)
                        continue
                    
                    # Login and navigate
                    log_message(task_id, f"{process_id}: Opening Facebook...")
                    driver.get('https://www.facebook.com/')
                    time.sleep(8)
                    
                    # Add cookies
                    if task.cookies and task.cookies[0]:
                        log_message(task_id, f"{process_id}: Adding cookies...")
                        cookie_pairs = task.cookies[0].split(';')
                        for cookie_pair in cookie_pairs:
                            if '=' in cookie_pair:
                                name, value = cookie_pair.strip().split('=', 1)
                                try:
                                    driver.add_cookie({'name': name, 'value': value, 'domain': '.facebook.com', 'path': '/'})
                                except:
                                    pass
                        driver.refresh()
                        time.sleep(5)
                    
                    # Open chat
                    chat_url = f'https://www.facebook.com/messages/t/{task.chat_id.strip()}'
                    log_message(task_id, f"{process_id}: Opening chat...")
                    driver.get(chat_url)
                    time.sleep(15)  # Critical: Wait for page to fully load
                    
                    # Find message input with retry
                    message_input = None
                    for retry in range(3):
                        message_input = self._find_message_input(driver, task_id, process_id)
                        if message_input:
                            break
                        log_message(task_id, f"{process_id}: Retry {retry+1}/3 finding input...")
                        time.sleep(5)
                    
                    if not message_input:
                        log_message(task_id, f"{process_id}: Failed to find input!")
                        driver.quit()
                        driver = None
                        time.sleep(15)
                        continue
                    
                    task.last_restart = datetime.now()
                    self.save_task(task)
                    log_message(task_id, f"{process_id}: ✅ Ready! Resuming from message #{task.messages_sent + 1}")
                    consecutive_failures = 0
                    time.sleep(3)
                
                # Verify input still works
                try:
                    if not message_input.is_enabled():
                        raise Exception("Input disabled")
                except:
                    log_message(task_id, f"{process_id}: Input lost, reconnecting...")
                    driver = None
                    continue
                
                # Send message (60+ seconds delay ensure karo)
                success = self._send_message(driver, message_input, task, task_id, process_id)
                
                if success:
                    consecutive_failures = 0
                    # Minimum 60 seconds delay
                    delay = max(60, task.delay)
                    log_message(task_id, f"{process_id}: Waiting {delay} seconds...")
                    
                    # Break delay into smaller chunks for stop flag checking
                    for _ in range(delay):
                        if task.stop_flag or task.status != "running":
                            break
                        time.sleep(1)
                else:
                    consecutive_failures += 1
                    log_message(task_id, f"{process_id}: Failed ({consecutive_failures}/3)")
                    if consecutive_failures >= 3:
                        driver = None
                        consecutive_failures = 0
                    time.sleep(30)
                
                # Memory cleanup every 30 messages
                if task.messages_sent % 30 == 0 and task.messages_sent > 0:
                    gc.collect()
                    log_message(task_id, f"{process_id}: 🧹 Memory cleanup done")
                
            except Exception as e:
                log_message(task_id, f"{process_id}: Error: {str(e)[:100]}")
                driver = None
                time.sleep(15)
        
        # Cleanup
        if driver:
            try:
                driver.quit()
            except:
                pass
        hard_kill_chromium(task_id)
        task.running = False

task_manager = TaskManager()

# ==================== FLASK WEB UI ====================
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'render-secret-key-change-this')

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# HTML Template (same as before)
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Facebook Message Bot - Control Panel</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container { max-width: 1400px; margin: 0 auto; }
        .header {
            background: white;
            border-radius: 10px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header h1 { color: #667eea; font-size: 24px; }
        .logout-btn {
            background: #dc3545;
            color: white;
            padding: 10px 20px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            text-decoration: none;
        }
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        .stat-card {
            background: white;
            border-radius: 10px;
            padding: 20px;
            text-align: center;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .stat-card h3 { color: #666; font-size: 14px; margin-bottom: 10px; }
        .stat-card .value { font-size: 32px; font-weight: bold; color: #667eea; }
        .main-content {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }
        .card {
            background: white;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }
        .card h2 {
            color: #333;
            margin-bottom: 20px;
            font-size: 20px;
            border-bottom: 2px solid #667eea;
            padding-bottom: 10px;
        }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; color: #666; font-weight: 500; }
        input, textarea, select {
            width: 100%;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 5px;
            font-size: 14px;
        }
        textarea { resize: vertical; min-height: 100px; }
        button {
            background: #667eea;
            color: white;
            padding: 10px 20px;
            border: none;
            border-radius: 5px;
            cursor: pointer;
            font-size: 14px;
            margin-right: 10px;
        }
        button:hover { background: #5a67d8; }
        button.danger { background: #dc3545; }
        button.success { background: #28a745; }
        button.warning { background: #ffc107; color: #333; }
        .task-list { margin-top: 20px; }
        .task-item {
            background: #f8f9fa;
            border-radius: 5px;
            padding: 15px;
            margin-bottom: 10px;
            border-left: 4px solid #667eea;
            cursor: pointer;
        }
        .task-item.running { border-left-color: #28a745; }
        .task-item.stopped { border-left-color: #dc3545; }
        .task-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .task-id { font-weight: bold; color: #333; }
        .task-status {
            padding: 3px 10px;
            border-radius: 3px;
            font-size: 12px;
            font-weight: bold;
        }
        .status-running { background: #d4edda; color: #155724; }
        .status-stopped { background: #f8d7da; color: #721c24; }
        .task-details { font-size: 12px; color: #666; margin-bottom: 10px; }
        .task-actions button { padding: 5px 10px; font-size: 12px; margin-right: 5px; }
        .logs {
            background: #1e1e1e;
            color: #d4d4d4;
            border-radius: 5px;
            padding: 15px;
            font-family: 'Courier New', monospace;
            font-size: 12px;
            height: 400px;
            overflow-y: auto;
        }
        .log-line { margin-bottom: 5px; white-space: pre-wrap; word-wrap: break-word; }
        .log-error { color: #f48771; }
        .refresh-btn { float: right; padding: 5px 10px; font-size: 12px; }
        @media (max-width: 768px) { .main-content { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🤖 Facebook Message Bot</h1>
            <a href="/logout" class="logout-btn">Logout</a>
        </div>
        
        <div class="stats">
            <div class="stat-card"><h3>Total Tasks</h3><div class="value" id="totalTasks">0</div></div>
            <div class="stat-card"><h3>Running Tasks</h3><div class="value" id="runningTasks">0</div></div>
            <div class="stat-card"><h3>Stopped Tasks</h3><div class="value" id="stoppedTasks">0</div></div>
            <div class="stat-card"><h3>Total Messages</h3><div class="value" id="totalMessages">0</div></div>
        </div>
        
        <div class="main-content">
            <div class="card">
                <h2>➕ Create New Task</h2>
                <form id="createTaskForm">
                    <div class="form-group">
                        <label>Chat Thread ID</label>
                        <input type="text" name="chat_id" required placeholder="e.g., 1362400298935018">
                    </div>
                    <div class="form-group">
                        <label>Name Prefix (optional)</label>
                        <input type="text" name="name_prefix" placeholder="e.g., John">
                    </div>
                    <div class="form-group">
                        <label>Messages (one per line)</label>
                        <textarea name="messages" required placeholder="Hello!&#10;How are you?&#10;Nice to meet you!"></textarea>
                    </div>
                    <div class="form-group">
                        <label>Delay (seconds) - Minimum 60</label>
                        <input type="number" name="delay" value="60" min="60">
                    </div>
                    <div class="form-group">
                        <label>Facebook Cookies</label>
                        <textarea name="cookies" required placeholder="c_user=1234567890; xs=789012%3Aabc123; datr=abc123"></textarea>
                    </div>
                    <button type="submit">Create & Start Task</button>
                </form>
            </div>
            
            <div class="card">
                <h2>📋 Tasks</h2>
                <div id="tasksList" class="task-list">Loading...</div>
            </div>
        </div>
        
        <div class="card">
            <h2>📄 Task Logs <button class="refresh-btn" onclick="refreshLogs()">Refresh</button></h2>
            <div class="logs" id="logsContainer"><div class="log-line">Select a task to view logs...</div></div>
        </div>
    </div>
    
    <script>
        let currentTaskId = null;
        
        function loadStats() {
            fetch('/api/stats').then(res => res.json()).then(data => {
                document.getElementById('totalTasks').textContent = data.total_tasks;
                document.getElementById('runningTasks').textContent = data.running_tasks;
                document.getElementById('stoppedTasks').textContent = data.stopped_tasks;
                document.getElementById('totalMessages').textContent = data.total_messages;
            });
        }
        
        function loadTasks() {
            fetch('/api/tasks').then(res => res.json()).then(tasks => {
                const container = document.getElementById('tasksList');
                if (tasks.length === 0) {
                    container.innerHTML = '<p style="text-align: center; color: #666;">No tasks created yet</p>';
                    return;
                }
                container.innerHTML = tasks.map(task => `
                    <div class="task-item ${task.status}" onclick="selectTask('${task.task_id}')">
                        <div class="task-header">
                            <span class="task-id">${task.task_id}</span>
                            <span class="task-status status-${task.status}">${task.status.toUpperCase()}</span>
                        </div>
                        <div class="task-details">
                            Chat: ${task.chat_id} | Sent: ${task.messages_sent} msgs | Uptime: ${task.uptime}
                        </div>
                        <div class="task-actions" onclick="event.stopPropagation()">
                            ${task.status === 'running' ? 
                                `<button class="warning" onclick="stopTask('${task.task_id}')">⏸ Stop</button>` :
                                `<button class="success" onclick="startTask('${task.task_id}')">▶ Start</button>`
                            }
                            <button class="danger" onclick="deleteTask('${task.task_id}')">🗑 Delete</button>
                        </div>
                    </div>
                `).join('');
            });
        }
        
        function selectTask(taskId) { currentTaskId = taskId; refreshLogs(); }
        
        function refreshLogs() {
            if (!currentTaskId) return;
            fetch(`/api/logs/${currentTaskId}`).then(res => res.json()).then(data => {
                const container = document.getElementById('logsContainer');
                if (data.logs.length === 0) {
                    container.innerHTML = '<div class="log-line">No logs available</div>';
                    return;
                }
                container.innerHTML = data.logs.map(log => {
                    const isError = log.includes('ERROR') || log.includes('Fatal');
                    return `<div class="log-line ${isError ? 'log-error' : ''}">${escapeHtml(log)}</div>`;
                }).join('');
                container.scrollTop = container.scrollHeight;
            });
        }
        
        function startTask(taskId) { fetch(`/api/tasks/${taskId}/start`, { method: 'POST' }).then(() => { loadTasks(); loadStats(); }); }
        function stopTask(taskId) { fetch(`/api/tasks/${taskId}/stop`, { method: 'POST' }).then(() => { loadTasks(); loadStats(); }); }
        
        function deleteTask(taskId) {
            if (confirm('Delete this task?')) {
                fetch(`/api/tasks/${taskId}`, { method: 'DELETE' }).then(() => {
                    if (currentTaskId === taskId) { currentTaskId = null; document.getElementById('logsContainer').innerHTML = '<div class="log-line">Select a task to view logs...</div>'; }
                    loadTasks(); loadStats();
                });
            }
        }
        
        function escapeHtml(text) { const div = document.createElement('div'); div.textContent = text; return div.innerHTML; }
        
        document.getElementById('createTaskForm').addEventListener('submit', (e) => {
            e.preventDefault();
            const formData = new FormData(e.target);
            const data = {
                chat_id: formData.get('chat_id'),
                name_prefix: formData.get('name_prefix'),
                messages: formData.get('messages').split('\\n').filter(m => m.trim()),
                delay: parseInt(formData.get('delay')),
                cookies: formData.get('cookies')
            };
            fetch('/api/tasks/create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(data)
            }).then(res => res.json()).then(result => {
                if (result.success) { alert('Task created!'); e.target.reset(); loadTasks(); loadStats(); }
                else { alert('Error: ' + result.error); }
            });
        });
        
        setInterval(() => { loadStats(); loadTasks(); if (currentTaskId) refreshLogs(); }, 3000);
        loadStats(); loadTasks();
    </script>
</body>
</html>
'''

LOGIN_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Login - Facebook Bot</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        .login-container {
            background: white;
            border-radius: 10px;
            padding: 40px;
            width: 350px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
        }
        h1 { color: #667eea; text-align: center; margin-bottom: 30px; }
        input { width: 100%; padding: 12px; margin-bottom: 15px; border: 1px solid #ddd; border-radius: 5px; }
        button { width: 100%; padding: 12px; background: #667eea; color: white; border: none; border-radius: 5px; cursor: pointer; }
        button:hover { background: #5a67d8; }
        .error { color: #dc3545; text-align: center; margin-top: 10px; }
        .info { text-align: center; margin-top: 20px; font-size: 12px; color: #666; }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>🤖 Bot Login</h1>
        <form method="POST">
            <input type="text" name="username" placeholder="Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
            {% if error %}<div class="error">{{ error }}</div>{% endif %}
        </form>
        <div class="info">Default: admin / admin123</div>
    </div>
</body>
</html>
'''

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        import hashlib
        username = request.form.get('username')
        password = request.form.get('password')
        password_hash = hashlib.sha256(password.encode()).hexdigest()
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE username = ? AND password_hash = ?', (username, password_hash))
        user = cursor.fetchone()
        conn.close()
        
        if user:
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('index'))
        else:
            return render_template_string(LOGIN_TEMPLATE, error='Invalid credentials')
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
@login_required
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/stats')
@login_required
def api_stats():
    tasks = task_manager.tasks.values()
    username = session.get('username')
    user_tasks = [t for t in tasks if t.username == username]
    return jsonify({
        'total_tasks': len(user_tasks),
        'running_tasks': sum(1 for t in user_tasks if t.status == 'running'),
        'stopped_tasks': sum(1 for t in user_tasks if t.status == 'stopped'),
        'total_messages': sum(t.messages_sent for t in user_tasks)
    })

@app.route('/api/tasks')
@login_required
def api_tasks():
    username = session.get('username')
    tasks = [t for t in task_manager.tasks.values() if t.username == username]
    return jsonify([{
        'task_id': t.task_id,
        'status': t.status,
        'chat_id': t.chat_id,
        'messages_sent': t.messages_sent,
        'uptime': t.get_uptime(),
        'delay': t.delay
    } for t in tasks])

@app.route('/api/tasks/create', methods=['POST'])
@login_required
def api_create_task():
    data = request.json
    username = session.get('username')
    
    try:
        task_id = f"task_{random.randint(10000, 99999)}"
        cookies = [data.get('cookies', '')]
        messages = data.get('messages', ['Hello!'])
        delay = int(data.get('delay', 60))
        if delay < 60:
            delay = 60  # Enforce minimum 60 seconds
        
        task = Task(
            task_id=task_id,
            username=username,
            cookies=cookies,
            chat_id=data.get('chat_id', ''),
            name_prefix=data.get('name_prefix', ''),
            messages=messages,
            delay=delay,
            status='stopped',
            messages_sent=0,
            start_time=None,
            last_active=None,
            last_restart=None,
            rotation_index=0
        )
        
        task_manager.tasks[task_id] = task
        task_manager.save_task(task)
        task_manager.start_task(task_id)
        
        return jsonify({'success': True, 'task_id': task_id})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/tasks/<task_id>/start', methods=['POST'])
@login_required
def api_start_task(task_id):
    if task_id not in task_manager.tasks:
        return jsonify({'error': 'Task not found'}), 404
    if task_manager.tasks[task_id].username != session.get('username'):
        return jsonify({'error': 'Unauthorized'}), 403
    if task_manager.start_task(task_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to start'}), 400

@app.route('/api/tasks/<task_id>/stop', methods=['POST'])
@login_required
def api_stop_task(task_id):
    if task_id not in task_manager.tasks:
        return jsonify({'error': 'Task not found'}), 404
    if task_manager.tasks[task_id].username != session.get('username'):
        return jsonify({'error': 'Unauthorized'}), 403
    if task_manager.stop_task(task_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to stop'}), 400

@app.route('/api/tasks/<task_id>', methods=['DELETE'])
@login_required
def api_delete_task(task_id):
    if task_id not in task_manager.tasks:
        return jsonify({'error': 'Task not found'}), 404
    if task_manager.tasks[task_id].username != session.get('username'):
        return jsonify({'error': 'Unauthorized'}), 403
    if task_manager.delete_task(task_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to delete'}), 400

@app.route('/api/logs/<task_id>')
@login_required
def api_logs(task_id):
    if task_id not in task_manager.tasks:
        return jsonify({'logs': []})
    if task_manager.tasks[task_id].username != session.get('username'):
        return jsonify({'logs': []})
    logs = list(task_logs.get(task_id, []))
    return jsonify({'logs': logs[-100:]})

@app.route('/health')
def health():
    return jsonify({'status': 'alive', 'tasks': len(task_manager.tasks), 'running': sum(1 for t in task_manager.tasks.values() if t.status == 'running')})

if __name__ == '__main__':
    print("=" * 60)
    print("🤖 Facebook Message Bot - Fixed for Render 512MB RAM")
    print("🖥️ Desktop User-Agent (Stable Facebook selectors)")
    print(f"🔄 Browser restarts every {BROWSER_RESTART_HOURS} hours")
    print(f"🔧 Max Tasks: {MAX_TASKS}")
    print(f"📍 Access at: http://localhost:{PORT}")
    print(f"🔑 Default login: admin / admin123")
    print("📨 Message delay: Minimum 60 seconds")
    print("=" * 60)
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
