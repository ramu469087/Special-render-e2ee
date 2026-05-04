from flask import Flask, render_template, request, jsonify
import os
import threading
import time
import json
import random
import sqlite3
import psutil
import gc
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass
from collections import deque

from cryptography.fernet import Fernet
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

app = Flask(__name__)

# Configuration
MAX_TASKS = 100
DB_PATH = Path(__file__).parent / 'bot_data.db'
ENCRYPTION_KEY_FILE = Path(__file__).parent / '.encryption_key'

# Store logs
task_logs = {}

def log_message(task_id: str, msg: str):
    timestamp = time.strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {msg}"
    
    if task_id not in task_logs:
        task_logs[task_id] = deque(maxlen=100)
    
    task_logs[task_id].append(formatted_msg)
    
    with open('bot.log', 'a') as f:
        f.write(f"{formatted_msg}\n")
    
    print(formatted_msg)

# Encryption setup
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

# Database setup
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
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
            current_cookie_index INTEGER DEFAULT 0,
            start_time TIMESTAMP,
            last_active TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    current_cookie_index: int
    start_time: Optional[datetime]
    last_active: Optional[datetime]
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
        self.start_memory_cleaner()
    
    def load_tasks_from_db(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT task_id, telegram_id, cookies_encrypted, chat_id, name_prefix, messages, 
                   delay, status, messages_sent, current_cookie_index, start_time, last_active
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
                    current_cookie_index=row[9] or 0,
                    start_time=datetime.fromisoformat(row[10]) if row[10] else None,
                    last_active=datetime.fromisoformat(row[11]) if row[11] else None
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
             delay, status, messages_sent, current_cookie_index, start_time, last_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            task.current_cookie_index,
            task.start_time.isoformat() if task.start_time else None,
            task.last_active.isoformat() if task.last_active else None
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
        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-setuid-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36')
        chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        
        chromium_paths = ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome', '/usr/bin/chrome']
        for chromium_path in chromium_paths:
            if Path(chromium_path).exists():
                chrome_options.binary_location = chromium_path
                log_message(task_id, f'Found Chromium at: {chromium_path}')
                break
        
        chromedriver_paths = ['/usr/bin/chromedriver', '/usr/local/bin/chromedriver']
        driver_path = None
        for driver_candidate in chromedriver_paths:
            if Path(driver_candidate).exists():
                driver_path = driver_candidate
                log_message(task_id, f'Found ChromeDriver at: {driver_path}')
                break
        
        try:
            from selenium.webdriver.chrome.service import Service
            
            if driver_path:
                service = Service(executable_path=driver_path)
                driver = webdriver.Chrome(service=service, options=chrome_options)
                log_message(task_id, 'Chrome started with detected ChromeDriver!')
            else:
                driver = webdriver.Chrome(options=chrome_options)
                log_message(task_id, 'Chrome started with default driver!')
            
            driver.set_window_size(1920, 1080)
            return driver
            
        except Exception as error:
            log_message(task_id, f'Browser setup failed: {error}')
            try:
                from webdriver_manager.chrome import ChromeDriverManager
                from selenium.webdriver.chrome.service import Service
                log_message(task_id, 'Trying webdriver-manager...')
                service = Service(ChromeDriverManager().install())
                driver = webdriver.Chrome(service=service, options=chrome_options)
                log_message(task_id, 'Chrome started with webdriver-manager!')
                return driver
            except Exception as e:
                log_message(task_id, f'All browser setups failed: {e}')
                raise error
    
    def _find_message_input(self, driver, task_id: str, process_id: str):
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
                                log_message(task_id, f"{process_id}: Using primary selector editable element")
                                return element
                            elif selector == '[contenteditable="true"]' or selector == 'textarea' or selector == 'input[type="text"]':
                                log_message(task_id, f"{process_id}: Using fallback editable element")
                                return element
                    except Exception:
                        continue
            except Exception:
                continue
        
        log_message(task_id, f"{process_id}: ❌ Message input not found!")
        return None
    
    def _run_task(self, task_id: str):
        task = self.tasks[task_id]
        task.running = True
        process_id = f"TASK-{task_id[-6:]}"
        
        while task.status == "running" and not task.stop_flag:
            try:
                self._send_messages(task, process_id)
            except Exception as e:
                log_message(task_id, f"ERROR: {str(e)[:100]}")
                time.sleep(5)
        
        task.running = False
        if task_id in self.task_threads:
            del self.task_threads[task_id]
    
    def _send_messages(self, task: Task, process_id: str):
        driver = None
        message_rotation_index = 0
        task_id = task.task_id
        
        try:
            log_message(task_id, f"{process_id}: Starting automation...")
            driver = self._setup_browser(task_id)
            
            log_message(task_id, f"{process_id}: Navigating to Facebook...")
            driver.get('https://www.facebook.com/')
            time.sleep(8)
            
            current_cookie = task.cookies[0] if task.cookies else ""
            
            if current_cookie and current_cookie.strip():
                log_message(task_id, f"{process_id}: Adding cookies...")
                cookie_array = current_cookie.split(';')
                for cookie in cookie_array:
                    cookie_trimmed = cookie.strip()
                    if cookie_trimmed:
                        first_equal_index = cookie_trimmed.find('=')
                        if first_equal_index > 0:
                            name = cookie_trimmed[:first_equal_index].strip()
                            value = cookie_trimmed[first_equal_index + 1:].strip()
                            try:
                                driver.add_cookie({
                                    'name': name,
                                    'value': value,
                                    'domain': '.facebook.com',
                                    'path': '/'
                                })
                            except Exception:
                                pass
            
            if task.chat_id:
                chat_id = task.chat_id.strip()
                normal_url = f'https://www.facebook.com/messages/t/{chat_id}'
                e2ee_url = f'https://www.facebook.com/messages/e2ee/t/{chat_id}'
                
                log_message(task_id, f"{process_id}: Trying E2EE URL: {e2ee_url}")
                driver.get(e2ee_url)
                time.sleep(5)
                
                current_url = driver.current_url
                if 'e2ee' in current_url:
                    log_message(task_id, f"{process_id}: ✅ Using E2EE URL")
                else:
                    log_message(task_id, f"{process_id}: Using Normal URL")
            else:
                log_message(task_id, f"{process_id}: Opening messages...")
                driver.get('https://www.facebook.com/messages')
            
            time.sleep(15)
            
            message_input = self._find_message_input(driver, task_id, process_id)
            
            if not message_input:
                task.status = "stopped"
                self.save_task(task)
                return
            
            delay = int(task.delay)
            messages_sent = 0
            messages_list = [msg.strip() for msg in task.messages if msg.strip()]
            
            if not messages_list:
                messages_list = ['Hello!']
            
            log_message(task_id, f"{process_id}: Starting infinite message loop...")
            
            while task.status == "running" and not task.stop_flag:
                base_message = messages_list[message_rotation_index % len(messages_list)]
                message_rotation_index += 1
                
                if task.name_prefix:
                    message_to_send = f"{task.name_prefix} {base_message}"
                else:
                    message_to_send = base_message
                
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
                        const sendButtons = document.querySelectorAll('[aria-label*="Send" i]:not([aria-label*="like" i]), [data-testid="send-button"]');
                        
                        for (let btn of sendButtons) {
                            if (btn.offsetParent !== null) {
                                btn.click();
                                return 'button_clicked';
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
                        log_message(task_id, f"{process_id}: ✅ Sent via Enter: \"{message_to_send[:30]}...\"")
                    else:
                        log_message(task_id, f"{process_id}: ✅ Sent via button: \"{message_to_send[:30]}...\"")
                    
                    messages_sent += 1
                    task.messages_sent = messages_sent
                    task.last_active = datetime.now()
                    self.save_task(task)
                    
                    log_message(task_id, f"{process_id}: Message #{messages_sent} sent. Waiting {delay}s...")
                    time.sleep(delay)
                    
                except Exception as e:
                    log_message(task_id, f"{process_id}: Send error: {str(e)[:100]}")
                    time.sleep(5)
            
            log_message(task_id, f"{process_id}: Automation stopped. Total messages: {messages_sent}")
            
        except Exception as e:
            log_message(task_id, f"{process_id}: Fatal error: {str(e)}")
            task.status = "stopped"
            self.save_task(task)
        finally:
            if driver:
                try:
                    driver.quit()
                    log_message(task_id, f"{process_id}: Browser closed")
                except:
                    pass
    
    def start_auto_resume(self):
        def auto_resume():
            while True:
                try:
                    for task_id, task in self.tasks.items():
                        if task.status == "running" and not task.running:
                            self.start_task(task_id)
                except Exception as e:
                    print(f"Auto resume error: {e}")
                time.sleep(60)
        
        threading.Thread(target=auto_resume, daemon=True).start()
    
    def start_memory_cleaner(self):
        def clean_memory():
            while True:
                time.sleep(3600)
                try:
                    gc.collect()
                    process = psutil.Process()
                    memory_mb = process.memory_info().rss / 1024 / 1024
                    log_message("SYSTEM", f"🧹 Memory cleaned: {memory_mb:.1f} MB used")
                except Exception as e:
                    print(f"Memory clean error: {e}")
        
        threading.Thread(target=clean_memory, daemon=True).start()

task_manager = TaskManager()

# Flask Routes
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/tasks')
def get_tasks():
    tasks = []
    for task_id, task in task_manager.tasks.items():
        tasks.append({
            'task_id': task.task_id,
            'status': task.status,
            'messages_sent': task.messages_sent,
            'uptime': task.get_uptime(),
            'cookies_count': len(task.cookies),
            'messages_count': len(task.messages),
            'name': f"Task_{task_id[-6:]}"
        })
    return jsonify(tasks)

@app.route('/api/task/<task_id>')
def get_task(task_id):
    if task_id not in task_manager.tasks:
        return jsonify({'error': 'Task not found'}), 404
    
    task = task_manager.tasks[task_id]
    logs = list(task_logs.get(task_id, []))
    
    return jsonify({
        'task_id': task.task_id,
        'status': task.status,
        'messages_sent': task.messages_sent,
        'uptime': task.get_uptime(),
        'chat_id': task.chat_id,
        'name_prefix': task.name_prefix,
        'delay': task.delay,
        'cookies': task.cookies,
        'messages': task.messages,
        'logs': logs[-30:]
    })

@app.route('/api/task/create', methods=['POST'])
def create_task():
    data = request.json
    
    task_id = f"rajmishra_{random.randint(10000, 99999)}"
    
    cookies = [c.strip() for c in data.get('cookies', '').split('\n') if c.strip()]
    messages = [m.strip() for m in data.get('messages', '').split('\n') if m.strip()]
    
    task = Task(
        task_id=task_id,
        telegram_id="web_user",
        cookies=cookies,
        chat_id=data.get('chat_id', ''),
        name_prefix=data.get('name_prefix', ''),
        messages=messages,
        delay=int(data.get('delay', 30)),
        status="stopped",
        messages_sent=0,
        current_cookie_index=0,
        start_time=None,
        last_active=None
    )
    
    task_manager.tasks[task_id] = task
    task_manager.save_task(task)
    
    return jsonify({'success': True, 'task_id': task_id})

@app.route('/api/task/<task_id>/start', methods=['POST'])
def start_task_route(task_id):
    if task_id not in task_manager.tasks:
        return jsonify({'error': 'Task not found'}), 404
    
    if task_manager.start_task(task_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Failed to start task'}), 400

@app.route('/api/task/<task_id>/stop', methods=['POST'])
def stop_task_route(task_id):
    if task_id not in task_manager.tasks:
        return jsonify({'error': 'Task not found'}), 404
    
    task_manager.stop_task(task_id)
    return jsonify({'success': True})

@app.route('/api/task/<task_id>/delete', methods=['DELETE'])
def delete_task_route(task_id):
    if task_id not in task_manager.tasks:
        return jsonify({'error': 'Task not found'}), 404
    
    task_manager.delete_task(task_id)
    return jsonify({'success': True})

@app.route('/api/stats')
def get_stats():
    running = len([t for t in task_manager.tasks.values() if t.status == 'running'])
    total = len(task_manager.tasks)
    total_messages = sum(t.messages_sent for t in task_manager.tasks.values())
    
    try:
        process = psutil.Process()
        memory_mb = process.memory_info().rss / 1024 / 1024
    except:
        memory_mb = 0
    
    return jsonify({
        'running_tasks': running,
        'total_tasks': total,
        'total_messages': total_messages,
        'memory_usage_mb': round(memory_mb, 1),
        'max_tasks': MAX_TASKS
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
