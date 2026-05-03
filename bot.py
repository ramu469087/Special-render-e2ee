# bot.py - RENDER OPTIMIZED VERSION 1 (COMPLETE WORKING)
# 512MB RAM pe smoothly chalega

import os
import sys
import asyncio
import threading
import time
import json
import random
import sqlite3
import gc
import subprocess
import signal
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
import logging
from dataclasses import dataclass
from collections import deque

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, CallbackContext
from cryptography.fernet import Fernet
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

# ==================== CONFIGURATION ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8724341708:AAGPbkqYn4p0MNvnlPA0_HwD1uruUUQ8q8Y")
OWNER_FB_LINK = "https://www.facebook.com/profile.php?id=61588381456245"
SECRET_KEY = "TERI MA KI CHUT MDC"
CODE = "03102003"
MAX_TASKS = 1
PORT = int(os.environ.get("PORT", 10000))

# Render 512MB RAM Optimized Settings
BROWSER_RESTART_HOURS = 3
MIN_DELAY_SECONDS = 30
MEMORY_CLEANUP_EVERY = 25

# Use /tmp for Render
DB_PATH = Path('/tmp/bot_data.db')
ENCRYPTION_KEY_FILE = Path('/tmp/.encryption_key')

# Store logs in memory only
task_logs = {}

def log_message(task_id: str, msg: str):
    timestamp = time.strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {msg}"
    
    if task_id not in task_logs:
        task_logs[task_id] = deque(maxlen=100)
    
    task_logs[task_id].append(formatted_msg)
    print(formatted_msg)

# ==================== HARD KILL FUNCTION ====================
def hard_kill_all_chromium(task_id: str = ""):
    """Force kill ALL chromium processes"""
    try:
        subprocess.run(['pkill', '-9', '-f', 'chromium'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=5)
        subprocess.run(['pkill', '-9', '-f', 'chromedriver'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=5)
        subprocess.run(['pkill', '-9', '-f', 'chrome'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=5)
        subprocess.run(['rm', '-rf', '/dev/shm/.org.chromium*'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=5)
        time.sleep(2)
        if task_id:
            log_message(task_id, "🔪 Hard kill completed - ports freed")
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
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id TEXT UNIQUE NOT NULL,
            username TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            secret_key_verified INTEGER DEFAULT 0
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT UNIQUE NOT NULL,
            telegram_id TEXT NOT NULL,
            cookies_encrypted TEXT,
            chat_id TEXT,
            name_prefix TEXT,
            messages TEXT,
            delay INTEGER DEFAULT 30,
            status TEXT DEFAULT 'stopped',
            messages_sent INTEGER DEFAULT 0,
            rotation_index INTEGER DEFAULT 0,
            current_cookie_index INTEGER DEFAULT 0,
            start_time TIMESTAMP,
            last_active TIMESTAMP,
            last_browser_restart TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (telegram_id) REFERENCES users(telegram_id)
        )
    ''')
    
    conn.commit()
    conn.close()

init_db()

@dataclass
class Task:
    task_id: str
    telegram_id: str
    cookies: List[str]
    chat_id: str
    name_prefix: str
    messages: List[str]
    delay: int
    status: str
    messages_sent: int
    rotation_index: int
    current_cookie_index: int
    start_time: Optional[datetime]
    last_active: Optional[datetime]
    last_browser_restart: Optional[datetime]
    running: bool = False
    stop_flag: bool = False
    
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
        self.start_auto_resume()
    
    def load_tasks_from_db(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT task_id, telegram_id, cookies_encrypted, chat_id, name_prefix, messages, 
                   delay, status, messages_sent, rotation_index, current_cookie_index, 
                   start_time, last_active, last_browser_restart
            FROM tasks
        ''')
        for row in cursor.fetchall():
            try:
                cookies = json.loads(decrypt_data(row[2])) if row[2] else []
                messages = json.loads(decrypt_data(row[5])) if row[5] else []
                
                task = Task(
                    task_id=row[0],
                    telegram_id=row[1],
                    cookies=cookies,
                    chat_id=row[3] or "",
                    name_prefix=row[4] or "",
                    messages=messages,
                    delay=row[6] or 30,
                    status=row[7] or "stopped",
                    messages_sent=row[8] or 0,
                    rotation_index=row[9] or 0,
                    current_cookie_index=row[10] or 0,
                    start_time=datetime.fromisoformat(row[11]) if row[11] else None,
                    last_active=datetime.fromisoformat(row[12]) if row[12] else None,
                    last_browser_restart=datetime.fromisoformat(row[13]) if row[13] else None
                )
                self.tasks[task.task_id] = task
            except Exception as e:
                print(f"Error loading task {row[0]}: {e}")
        conn.close()
    
    def save_task(self, task: Task):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO tasks 
            (task_id, telegram_id, cookies_encrypted, chat_id, name_prefix, messages, 
             delay, status, messages_sent, rotation_index, current_cookie_index, 
             start_time, last_active, last_browser_restart)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task.task_id,
            task.telegram_id,
            encrypt_data(json.dumps(task.cookies)),
            task.chat_id,
            task.name_prefix,
            encrypt_data(json.dumps(task.messages)),
            task.delay,
            task.status,
            task.messages_sent,
            task.rotation_index,
            task.current_cookie_index,
            task.start_time.isoformat() if task.start_time else None,
            task.last_active.isoformat() if task.last_active else None,
            task.last_browser_restart.isoformat() if task.last_browser_restart else None
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
            return False
        task.status = "running"
        task.stop_flag = False
        if not task.start_time:
            task.start_time = datetime.now()
        if not task.last_browser_restart:
            task.last_browser_restart = datetime.now()
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
        """Setup Chrome browser - Version 1 style (working on Render)"""
        hard_kill_all_chromium(task_id)
        
        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-setuid-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--disable-plugins')
        chrome_options.add_argument('--window-size=1280,720')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36')
        
        # Memory optimization for 512MB RAM
        chrome_options.add_argument('--memory-pressure-off')
        chrome_options.add_argument('--max_old_space_size=128')
        chrome_options.add_argument('--js-flags="--max-old-space-size=128"')
        chrome_options.add_argument('--disable-software-rasterizer')
        
        # Ghost mode
        chrome_options.add_experimental_option('excludeSwitches', ['enable-logging', 'enable-automation'])
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        
        # Crash prevention
        chrome_options.add_argument('--disable-crash-reporter')
        chrome_options.add_argument('--disable-breakpad')
        
        # Render specific - Chrome location
        chrome_paths = [
            '/usr/bin/chromium',
            '/usr/bin/chromium-browser',
            '/usr/bin/google-chrome',
            '/usr/bin/chrome'
        ]
        
        for chrome_path in chrome_paths:
            if Path(chrome_path).exists():
                chrome_options.binary_location = chrome_path
                log_message(task_id, f'Found Chrome at: {chrome_path}')
                break
        
        try:
            # Try system chromedriver first (Version 1 style)
            chromedriver_paths = [
                '/usr/bin/chromedriver',
                '/usr/local/bin/chromedriver'
            ]
            
            for driver_path in chromedriver_paths:
                if Path(driver_path).exists():
                    log_message(task_id, f'Found ChromeDriver at: {driver_path}')
                    service = Service(executable_path=driver_path, service_log_path='/dev/null')
                    driver = webdriver.Chrome(service=service, options=chrome_options)
                    driver.set_window_size(1280, 720)
                    driver.set_page_load_timeout(30)
                    driver.set_script_timeout(30)
                    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                    log_message(task_id, '✅ Chrome browser setup completed!')
                    return driver
            
            # Fallback to webdriver-manager (NO ChromeType - Version 1 style)
            from webdriver_manager.chrome import ChromeDriverManager
            log_message(task_id, 'Trying webdriver-manager...')
            driver_path = ChromeDriverManager().install()
            service = Service(executable_path=driver_path, service_log_path='/dev/null')
            driver = webdriver.Chrome(service=service, options=chrome_options)
            driver.set_window_size(1280, 720)
            driver.set_page_load_timeout(30)
            driver.set_script_timeout(30)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            log_message(task_id, '✅ Chrome browser setup completed!')
            return driver
            
        except Exception as error:
            log_message(task_id, f'Browser setup failed: {error}')
            hard_kill_all_chromium(task_id)
            raise error
    
    def _find_message_input(self, driver, task_id: str, process_id: str):
        """All 12 selectors with keyword matching - Version 1 style"""
        log_message(task_id, f"{process_id}: Finding message input...")
        
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2)
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(2)
        except Exception:
            pass
        
        message_input_selectors = [
            'div[contenteditable="true"][role="textbox"]',
            'div[contenteditable="true"][data-lexical-editor="true"]',
            'div[aria-label*="message" i][contenteditable="true"]',
            'div[aria-label*="Message" i][contenteditable="true"]',
            'div[contenteditable="true"][spellcheck="true"]',
            '[role="textbox"][contenteditable="true"]',
            'textarea[placeholder*="message" i]',
            'div[aria-placeholder*="message" i]',
            'div[data-placeholder*="message" i]',
            '[contenteditable="true"]',
            'textarea',
            'input[type="text"]'
        ]
        
        for idx, selector in enumerate(message_input_selectors):
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for element in elements:
                    try:
                        is_editable = driver.execute_script("""
                            return arguments[0].contentEditable === 'true' || 
                                   arguments[0].tagName === 'TEXTAREA' || 
                                   arguments[0].tagName === 'INPUT';
                        """, element)
                        
                        if is_editable:
                            try:
                                element.click()
                                time.sleep(0.5)
                            except:
                                pass
                            
                            element_text = driver.execute_script("return arguments[0].placeholder || arguments[0].getAttribute('aria-label') || arguments[0].getAttribute('aria-placeholder') || '';", element).lower()
                            
                            keywords = ['message', 'write', 'type', 'send', 'chat', 'msg', 'reply', 'text', 'aa']
                            if any(keyword in element_text for keyword in keywords):
                                log_message(task_id, f"{process_id}: ✅ Found message input")
                                return element
                            elif idx < 10:
                                log_message(task_id, f"{process_id}: Using primary selector")
                                return element
                            elif selector == '[contenteditable="true"]' or selector == 'textarea' or selector == 'input[type="text"]':
                                log_message(task_id, f"{process_id}: Using fallback")
                                return element
                    except Exception:
                        continue
            except Exception:
                continue
        
        log_message(task_id, f"{process_id}: ❌ Message input not found!")
        return None
    
    def _login_and_navigate(self, driver, task: Task, task_id: str, process_id: str):
        """Login and navigate to chat"""
        log_message(task_id, f"{process_id}: Navigating to Facebook...")
        driver.get('https://www.facebook.com/')
        time.sleep(8)
        
        current_cookie = task.cookies[0] if task.cookies else ""
        if current_cookie and current_cookie.strip():
            log_message(task_id, f"{process_id}: Adding cookies...")
            cookie_array = current_cookie.split(';')
            for cookie in cookie_array:
                cookie_trimmed = cookie.strip()
                if cookie_trimmed and '=' in cookie_trimmed:
                    name, value = cookie_trimmed.split('=', 1)
                    try:
                        driver.add_cookie({
                            'name': name.strip(),
                            'value': value.strip(),
                            'domain': '.facebook.com',
                            'path': '/'
                        })
                    except:
                        pass
            driver.refresh()
            time.sleep(5)
        
        if task.chat_id:
            log_message(task_id, f"{process_id}: Opening conversation {task.chat_id}...")
            driver.get(f'https://www.facebook.com/messages/t/{task.chat_id.strip()}')
        else:
            log_message(task_id, f"{process_id}: Opening messages...")
            driver.get('https://www.facebook.com/messages')
        
        time.sleep(12)
        message_input = self._find_message_input(driver, task_id, process_id)
        return message_input
    
    def _send_single_message(self, driver, message_input, task: Task, task_id: str, process_id: str):
        """Send message - with 3 Enter events (Version 1 style)"""
        messages_list = [msg.strip() for msg in task.messages if msg.strip()]
        if not messages_list:
            messages_list = ['Hello!']
        
        msg_idx = task.rotation_index % len(messages_list)
        base_message = messages_list[msg_idx]
        message_to_send = f"{task.name_prefix} {base_message}" if task.name_prefix else base_message
        
        try:
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
                element.dispatchEvent(new InputEvent('input', { bubbles: true, data: message }));
            """, message_input, message_to_send)
            
            time.sleep(1)
            
            sent = driver.execute_script("""
                const sendSelectors = [
                    '[aria-label*="Send" i]:not([aria-label*="like" i])',
                    '[data-testid="send-button"]',
                    '[aria-label*="Envoyer" i]',
                    'button[type="submit"]',
                    'div[role="button"][aria-label*="Send" i]'
                ];
                
                for (const selector of sendSelectors) {
                    const btns = document.querySelectorAll(selector);
                    for (let btn of btns) {
                        if (btn.offsetParent !== null) {
                            btn.click();
                            return 'button_clicked';
                        }
                    }
                }
                return 'button_not_found';
            """)
            
            if sent == 'button_not_found':
                driver.execute_script("""
                    const element = arguments[0];
                    element.focus();
                    
                    const events = [
                        new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }),
                        new KeyboardEvent('keypress', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }),
                        new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true })
                    ];
                    
                    events.forEach(event => element.dispatchEvent(event));
                """, message_input)
                log_message(task_id, f"{process_id}: ✅ Sent via Enter (3 events)")
            else:
                log_message(task_id, f"{process_id}: ✅ Sent via button")
            
            task.messages_sent += 1
            task.rotation_index += 1
            task.last_active = datetime.now()
            self.save_task(task)
            
            log_message(task_id, f"{process_id}: Message #{task.messages_sent} sent. Rotation: {task.rotation_index}")
            return True
            
        except Exception as send_error:
            log_message(task_id, f"{process_id}: Send error: {str(send_error)[:100]}")
            return False
    
    def _run_task(self, task_id: str):
        """Main task runner"""
        task = self.tasks[task_id]
        task.running = True
        process_id = f"TASK-{task_id[-6:]}"
        
        driver = None
        message_input = None
        consecutive_failures = 0
        
        while task.status == "running" and not task.stop_flag:
            try:
                current_time = datetime.now()
                last_restart = task.last_browser_restart
                
                if last_restart:
                    hours_since_restart = (current_time - last_restart).total_seconds() / 3600
                else:
                    hours_since_restart = BROWSER_RESTART_HOURS + 1
                
                if hours_since_restart >= BROWSER_RESTART_HOURS or driver is None:
                    log_message(task_id, f"{process_id}: 🔄 Browser restart after {hours_since_restart:.1f} hours...")
                    
                    if driver:
                        try:
                            driver.quit()
                        except:
                            pass
                    
                    hard_kill_all_chromium(task_id)
                    
                    new_driver = None
                    for retry in range(3):
                        try:
                            new_driver = self._setup_browser(task_id)
                            if new_driver:
                                break
                        except Exception as e:
                            log_message(task_id, f"{process_id}: Setup retry {retry+1}/3 failed: {str(e)[:50]}")
                            hard_kill_all_chromium(task_id)
                            time.sleep(5)
                    
                    if not new_driver:
                        log_message(task_id, f"{process_id}: ❌ Failed to setup browser!")
                        time.sleep(30)
                        continue
                    
                    driver = new_driver
                    
                    for retry in range(3):
                        message_input = self._login_and_navigate(driver, task, task_id, process_id)
                        if message_input:
                            break
                        log_message(task_id, f"{process_id}: Navigate retry {retry+1}/3...")
                        time.sleep(5)
                    
                    if not message_input:
                        log_message(task_id, f"{process_id}: ❌ Failed to find message input!")
                        driver = None
                        hard_kill_all_chromium(task_id)
                        time.sleep(15)
                        continue
                    
                    task.last_browser_restart = datetime.now()
                    self.save_task(task)
                    
                    log_message(task_id, f"{process_id}: ✅ Browser ready! Resuming from message #{task.messages_sent + 1}")
                    consecutive_failures = 0
                    time.sleep(3)
                
                try:
                    if message_input:
                        message_input.is_enabled()
                    else:
                        raise Exception("Message input lost")
                except:
                    log_message(task_id, f"{process_id}: Input lost, reconnecting...")
                    message_input = self._login_and_navigate(driver, task, task_id, process_id)
                    if not message_input:
                        driver = None
                        time.sleep(5)
                        continue
                
                success = self._send_single_message(driver, message_input, task, task_id, process_id)
                
                if success:
                    consecutive_failures = 0
                    log_message(task_id, f"{process_id}: Waiting {task.delay}s for next message...")
                    time.sleep(task.delay)
                else:
                    consecutive_failures += 1
                    log_message(task_id, f"{process_id}: Send failed ({consecutive_failures}/3)")
                    
                    if consecutive_failures >= 3:
                        log_message(task_id, f"{process_id}: Too many failures, restarting...")
                        driver = None
                        consecutive_failures = 0
                    time.sleep(10)
                
                if task.messages_sent % MEMORY_CLEANUP_EVERY == 0 and task.messages_sent > 0:
                    try:
                        driver.execute_script("""
                            try {
                                localStorage.clear();
                                sessionStorage.clear();
                                if(window.gc) window.gc();
                            } catch(e) { }
                        """)
                        gc.collect()
                        log_message(task_id, f"{process_id}: 🧹 Memory cleanup done")
                    except:
                        pass
                
            except Exception as e:
                log_message(task_id, f"{process_id}: Error: {str(e)[:100]}")
                driver = None
                hard_kill_all_chromium(task_id)
                time.sleep(10)
        
        if driver:
            try:
                driver.quit()
            except:
                pass
        hard_kill_all_chromium(task_id)
        task.running = False
        if task_id in self.task_threads:
            del self.task_threads[task_id]
    
    def start_auto_resume(self):
        def auto_resume():
            while True:
                try:
                    for task_id, task in self.tasks.items():
                        if task.status == "running" and not task.running:
                            log_message(task_id, f"🔄 Auto-resuming task...")
                            hard_kill_all_chromium(task_id)
                            self.start_task(task_id)
                except Exception as e:
                    print(f"Auto resume error: {e}")
                time.sleep(60)
        
        thread = threading.Thread(target=auto_resume, daemon=True)
        thread.start()

task_manager = TaskManager()

# ==================== TELEGRAM BOT HANDLERS ====================
def verify_user(telegram_id: str, secret_key: str = None) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    if secret_key:
        if secret_key == SECRET_KEY:
            cursor.execute('INSERT OR REPLACE INTO users (telegram_id, secret_key_verified) VALUES (?, ?)', (telegram_id, 1))
            conn.commit()
            conn.close()
            return True
        return False
    
    cursor.execute('SELECT secret_key_verified FROM users WHERE telegram_id = ?', (telegram_id,))
    result = cursor.fetchone()
    conn.close()
    return result and result[0] == 1

async def start_command(update: Update, context: CallbackContext):
    user_id = str(update.effective_user.id)
    if verify_user(user_id):
        await show_menu(update, context)
    else:
        await update.message.reply_text(
            f"Welcome to Raj Mishra end to end world\n\n"
            f"Please contact my owner: {OWNER_FB_LINK}\n\n"
            f"Send the secret key to continue:"
        )

async def handle_secret_key(update: Update, context: CallbackContext):
    user_id = str(update.effective_user.id)
    secret = update.message.text.strip()
    
    if verify_user(user_id, secret):
        await update.message.reply_text(
            "Welcome to New world\n\n"
            "Please choose option:\n\n"
            "A. Send cookies (one per line for multiple cookies)\n"
            "B. Send chat thread ID\n"
            "C. Send messages file (.txt)\n"
            "D. Send name prefix\n"
            "E. Send time delay\n"
            "F. Send code to start task\n"
            "G. Manage tasks\n\n"
            "Send the option letter to proceed:"
        )
        context.user_data['verified'] = True
        context.user_data['setup_step'] = 'awaiting_option'
    else:
        await update.message.reply_text(f"Code galat hai! Please visit my owner: {OWNER_FB_LINK}")

async def handle_option(update: Update, context: CallbackContext):
    option = update.message.text.strip().upper()
    
    if option == 'A':
        context.user_data['setup_step'] = 'awaiting_cookies'
        await update.message.reply_text("Send your Facebook cookies (one per line):")
    elif option == 'B':
        context.user_data['setup_step'] = 'awaiting_chat_id'
        await update.message.reply_text("Send chat thread ID:")
    elif option == 'C':
        context.user_data['setup_step'] = 'awaiting_messages'
        await update.message.reply_text("Send your messages file (.txt):")
    elif option == 'D':
        context.user_data['setup_step'] = 'awaiting_name_prefix'
        await update.message.reply_text("Send the name prefix:")
    elif option == 'E':
        context.user_data['setup_step'] = 'awaiting_delay'
        await update.message.reply_text("Send delay in seconds (minimum 30):")
    elif option == 'F':
        context.user_data['setup_step'] = 'awaiting_code'
        await update.message.reply_text("Send the code to start:")
    elif option == 'G':
        context.user_data['setup_step'] = 'awaiting_task_action'
        await update.message.reply_text(
            "Commands:\n"
            "/stop TASK_ID - Stop\n"
            "/resume TASK_ID - Resume\n"
            "/status TASK_ID - Status\n"
            "/delete TASK_ID - Delete\n"
            "/uptime TASK_ID - Uptime\n"
            "/logs TASK_ID - Logs\n"
            "/tasks - List all"
        )
    else:
        await update.message.reply_text("Invalid option!")

async def handle_cookies(update: Update, context: CallbackContext):
    text = update.message.text.strip()
    cookies = [c.strip() for c in text.split('\n') if c.strip()]
    
    if 'config' not in context.user_data:
        context.user_data['config'] = {}
    context.user_data['config']['cookies'] = cookies
    
    await update.message.reply_text(f"✅ {len(cookies)} cookie(s) saved!")
    context.user_data['setup_step'] = 'awaiting_option'
    await show_menu(update, context)

async def handle_chat_id(update: Update, context: CallbackContext):
    context.user_data['config']['chat_id'] = update.message.text.strip()
    await update.message.reply_text(f"✅ Chat ID saved!")
    context.user_data['setup_step'] = 'awaiting_option'
    await show_menu(update, context)

async def handle_messages(update: Update, context: CallbackContext):
    if update.message.document:
        file = await update.message.document.get_file()
        file_content = await file.download_as_bytearray()
        messages = file_content.decode('utf-8').strip().split('\n')
        messages = [m.strip() for m in messages if m.strip()]
        
        context.user_data['config']['messages'] = messages
        await update.message.reply_text(f"✅ {len(messages)} message(s) loaded!")
        context.user_data['setup_step'] = 'awaiting_option'
        await show_menu(update, context)
    else:
        await update.message.reply_text("Please send as .txt file!")

async def handle_name_prefix(update: Update, context: CallbackContext):
    context.user_data['config']['name_prefix'] = update.message.text.strip()
    await update.message.reply_text("✅ Name prefix saved!")
    context.user_data['setup_step'] = 'awaiting_option'
    await show_menu(update, context)

async def handle_delay(update: Update, context: CallbackContext):
    try:
        delay = int(update.message.text.strip())
        if delay < MIN_DELAY_SECONDS:
            await update.message.reply_text(f"⚠️ Using minimum {MIN_DELAY_SECONDS} seconds")
            delay = MIN_DELAY_SECONDS
        context.user_data['config']['delay'] = delay
        await update.message.reply_text(f"✅ Delay set to {delay} seconds!")
        context.user_data['setup_step'] = 'awaiting_option'
        await show_menu(update, context)
    except:
        await update.message.reply_text("Invalid number!")

async def handle_code(update: Update, context: CallbackContext):
    user_id = str(update.effective_user.id)
    code = update.message.text.strip()
    
    if code == CODE:
        config = context.user_data.get('config', {})
        
        required = ['cookies', 'chat_id', 'messages', 'name_prefix', 'delay']
        if not all(k in config for k in required):
            await update.message.reply_text("Complete all steps (A-E) first!")
            return
        
        task_id = f"rajmishra_{random.randint(10000, 99999)}"
        
        task = Task(
            task_id=task_id,
            telegram_id=user_id,
            cookies=config['cookies'],
            chat_id=config['chat_id'],
            name_prefix=config['name_prefix'],
            messages=config['messages'],
            delay=config['delay'],
            status="stopped",
            messages_sent=0,
            rotation_index=0,
            current_cookie_index=0,
            start_time=None,
            last_active=None,
            last_browser_restart=None
        )
        
        task_manager.tasks[task_id] = task
        task_manager.save_task(task)
        task_manager.start_task(task_id)
        
        await update.message.reply_text(
            f"✅ Task started!\n\n"
            f"Task ID: {task_id}\n"
            f"Browser Restart: Every {BROWSER_RESTART_HOURS} hours\n"
            f"Use /logs {task_id} to see live output"
        )
        
        context.user_data['config'] = {}
        context.user_data['setup_step'] = 'awaiting_option'
        await show_menu(update, context)
    else:
        await update.message.reply_text(f"❌ Wrong code!")

async def show_menu(update: Update, context: CallbackContext):
    menu = (
        "📋 Main Menu:\n\n"
        "A. Send cookies\n"
        "B. Send chat ID\n"
        "C. Send messages file\n"
        "D. Send name prefix\n"
        "E. Send delay\n"
        "F. Send code to start\n"
        "G. Manage tasks\n\n"
        "Send option letter:"
    )
    await update.message.reply_text(menu)

async def stop_task_command(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Usage: /stop TASK_ID")
        return
    task_id = context.args[0]
    user_id = str(update.effective_user.id)
    if task_id not in task_manager.tasks:
        await update.message.reply_text("Task not found!")
        return
    if task_manager.tasks[task_id].telegram_id != user_id:
        await update.message.reply_text("Not your task!")
        return
    if task_manager.stop_task(task_id):
        await update.message.reply_text(f"✅ Task {task_id} stopped!")

async def resume_task_command(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Usage: /resume TASK_ID")
        return
    task_id = context.args[0]
    user_id = str(update.effective_user.id)
    if task_id not in task_manager.tasks:
        await update.message.reply_text("Task not found!")
        return
    if task_manager.tasks[task_id].telegram_id != user_id:
        await update.message.reply_text("Not your task!")
        return
    if task_manager.start_task(task_id):
        await update.message.reply_text(f"✅ Task {task_id} resumed!")

async def status_task_command(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Usage: /status TASK_ID")
        return
    task_id = context.args[0]
    user_id = str(update.effective_user.id)
    if task_id not in task_manager.tasks:
        await update.message.reply_text("Task not found!")
        return
    task = task_manager.tasks[task_id]
    if task.telegram_id != user_id:
        await update.message.reply_text("Not your task!")
        return
    
    status_text = (
        f"📊 Task: {task_id}\n\n"
        f"Status: {task.status}\n"
        f"Messages Sent: {task.messages_sent}\n"
        f"Rotation: {task.rotation_index}\n"
        f"Delay: {task.delay}s\n"
        f"Uptime: {task.get_uptime()}"
    )
    await update.message.reply_text(status_text)

async def delete_task_command(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Usage: /delete TASK_ID")
        return
    task_id = context.args[0]
    user_id = str(update.effective_user.id)
    if task_id not in task_manager.tasks:
        await update.message.reply_text("Task not found!")
        return
    if task_manager.tasks[task_id].telegram_id != user_id:
        await update.message.reply_text("Not your task!")
        return
    if task_manager.delete_task(task_id):
        await update.message.reply_text(f"✅ Task {task_id} deleted!")

async def uptime_task_command(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Usage: /uptime TASK_ID")
        return
    task_id = context.args[0]
    user_id = str(update.effective_user.id)
    if task_id not in task_manager.tasks:
        await update.message.reply_text("Task not found!")
        return
    task = task_manager.tasks[task_id]
    if task.telegram_id != user_id:
        await update.message.reply_text("Not your task!")
        return
    await update.message.reply_text(f"⏱️ Uptime: {task.get_uptime()}")

async def logs_command(update: Update, context: CallbackContext):
    if not context.args:
        await update.message.reply_text("Usage: /logs TASK_ID")
        return
    task_id = context.args[0]
    user_id = str(update.effective_user.id)
    if task_id not in task_manager.tasks:
        await update.message.reply_text("Task not found!")
        return
    task = task_manager.tasks[task_id]
    if task.telegram_id != user_id:
        await update.message.reply_text("Not your task!")
        return
    
    logs = task_logs.get(task_id, [])
    if not logs:
        await update.message.reply_text("No logs yet.")
        return
    
    logs_text = "📺 LIVE CONSOLE (Last 30):\n\n"
    for log in list(logs)[-30:]:
        logs_text += f"{log}\n"
    
    logs_text += f"\n📈 Messages: {task.messages_sent} | Uptime: {task.get_uptime()}"
    
    if len(logs_text) > 4000:
        await update.message.reply_text(logs_text[:3500])
        await update.message.reply_text(logs_text[3500:])
    else:
        await update.message.reply_text(logs_text)

async def list_tasks_command(update: Update, context: CallbackContext):
    user_id = str(update.effective_user.id)
    user_tasks = [t for t in task_manager.tasks.values() if t.telegram_id == user_id]
    
    if not user_tasks:
        await update.message.reply_text("No tasks!")
        return
    
    tasks_list = "📋 Your Tasks:\n\n"
    for task in user_tasks:
        tasks_list += f"ID: {task.task_id}\n   Status: {task.status}\n   Sent: {task.messages_sent}\n   Uptime: {task.get_uptime()}\n---\n"
    
    await update.message.reply_text(tasks_list)

def health_check():
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', PORT))
    sock.listen(5)
    while True:
        try:
            client, _ = sock.accept()
            client.send(b"HTTP/1.1 200 OK\r\n\r\nOK")
            client.close()
        except:
            pass

async def handle_message(update: Update, context: CallbackContext):
    user_id = str(update.effective_user.id)
    text = update.message.text.strip()
    
    if not verify_user(user_id) and text != SECRET_KEY:
        await start_command(update, context)
        return
    
    if text == SECRET_KEY:
        await handle_secret_key(update, context)
        return
    
    step = context.user_data.get('setup_step', 'awaiting_option')
    
    if step == 'awaiting_option':
        await handle_option(update, context)
    elif step == 'awaiting_cookies':
        await handle_cookies(update, context)
    elif step == 'awaiting_chat_id':
        await handle_chat_id(update, context)
    elif step == 'awaiting_name_prefix':
        await handle_name_prefix(update, context)
    elif step == 'awaiting_delay':
        await handle_delay(update, context)
    elif step == 'awaiting_code':
        await handle_code(update, context)
    else:
        await show_menu(update, context)

def main():
    threading.Thread(target=health_check, daemon=True).start()
    
    application = Application.builder().token(BOT_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stop", stop_task_command))
    application.add_handler(CommandHandler("resume", resume_task_command))
    application.add_handler(CommandHandler("status", status_task_command))
    application.add_handler(CommandHandler("delete", delete_task_command))
    application.add_handler(CommandHandler("uptime", uptime_task_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CommandHandler("tasks", list_tasks_command))
    
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_messages))
    
    print("=" * 60)
    print("🚀 R4J M1SHR4 Bot Started on Render!")
    print(f"📱 Browser restart every {BROWSER_RESTART_HOURS} hours")
    print("🔪 Hard kill enabled")
    print(f"🧹 Memory cleanup every {MEMORY_CLEANUP_EVERY} messages")
    print("=" * 60)
    
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
