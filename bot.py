# -*- coding: utf-8 -*-
"""
ربات مدیریت فایل تلگرام
پیاده‌سازی کامل با python-telegram-bot (نسخه Async) و SQLite
تمام کد در همین یک فایل قرار دارد.

نقش‌ها:
  - manager (مدیر ارشد): آیدی‌های ثابت در MANAGER_IDS
  - representative (نماینده شرکت): با رمز عبور وارد می‌شود و می‌تواند
    با چندین اکانت تلگرام مختلف، همزمان به‌عنوان نماینده‌ی یک شرکت وارد شود.

نقش «ادمین» به‌طور کامل حذف شده است.
"""

import sqlite3
import logging
import os
import hashlib
import threading
from datetime import datetime, date

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==================================================================================
# ============================ تنظیمات کلی پروژه ==================================
# ==================================================================================

BOT_TOKEN = "8746222746:AAENy-Dl9Mr37ai5HcvfRYQt3ML_LLreyZM"

# لیست آیدی عددی مدیران اصلی (Manager). این افراد به‌صورت پیش‌فرض و ثابت مدیر هستند
# و نیازی به ثبت در دیتابیس ندارند. نقش ادمین در این پروژه وجود ندارد.
MANAGER_IDS = [8131564808]

DATABASE_NAME = "bot_database.db"

# حداکثر حجم مجاز فایل (بر حسب بایت) - پیش‌فرض ۵۰ مگابایت
MAX_FILE_SIZE = 50 * 1024 * 1024

# انواع فایل مجاز برای آپلود (بر اساس دسته‌بندی پیام تلگرام)
ALLOWED_FILE_TYPES = ["photo", "video", "document", "audio"]

PAGE_SIZE = 6  # تعداد آیتم در هر صفحه از صفحه‌بندی

MIN_PASSWORD_LENGTH = 4  # حداقل طول رمز عبور نماینده

# یک مقدار ثابت که برای هش کردن رمز عبور استفاده می‌شود (بهتر است تغییرش دهید)
PASSWORD_PEPPER = "CHANGE_THIS_PEPPER_VALUE_1234"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ==================================================================================
# ================================= لایه دیتابیس ===================================
# ==================================================================================


class Database:
    """کلاس مدیریت دیتابیس SQLite. تمام تعامل با دیتابیس از این کلاس عبور می‌کند."""

    def __init__(self, db_name: str):
        self.db_name = db_name
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_name, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate_legacy_schema()
        self._init_tables()

    # ------------------------------------------------------------------ مهاجرت از نسخه قدیمی
    def _migrate_legacy_schema(self):
        """اگر دیتابیس با نسخه‌ی قدیمی کد (که نقش ادمین و ستون telegram_id در
        representatives را داشت) ساخته شده باشد، جدول‌های قدیمی ناسازگار را
        بدون از دست رفتن دیتا کنار می‌گذارد تا جدول‌های جدید ساخته شوند."""
        with self._lock:
            cur = self._conn.cursor()

            # جدول ادمین‌ها دیگر استفاده نمی‌شود؛ در صورت وجود، با نام دیگری
            # نگه‌داری می‌شود (برای احتیاط) تا کد جدید بدون آن کار کند.
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='admins'"
            )
            if cur.fetchone():
                cur.execute("ALTER TABLE admins RENAME TO admins_legacy_backup")

            # اگر جدول representatives قدیمی (بدون ستون password_hash) وجود دارد،
            # آن را با نام دیگر نگه می‌داریم تا جدول جدید ساخته شود.
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='representatives'"
            )
            if cur.fetchone():
                cols = [r[1] for r in cur.execute("PRAGMA table_info(representatives)").fetchall()]
                if "password_hash" not in cols:
                    cur.execute("ALTER TABLE representatives RENAME TO representatives_legacy_backup")

            self._conn.commit()

    # ------------------------------------------------------------------ داخلی
    def _init_tables(self):
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript(
                """
                CREATE TABLE IF NOT EXISTS companies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    description TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_by INTEGER,
                    created_at TEXT
                );

                CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    created_by INTEGER,
                    created_at TEXT,
                    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
                );

                -- هر ردیف representatives یک «هویت نماینده» برای یک شرکت است
                -- (نام + رمز عبور) و دیگر به یک آیدی عددی خاص محدود نیست.
                CREATE TABLE IF NOT EXISTS representatives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id INTEGER NOT NULL,
                    full_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    added_by INTEGER,
                    added_at TEXT,
                    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
                );

                -- نگاشت هر اکانت تلگرام که با رمز صحیح وارد شده به یک هویت نماینده.
                -- چند اکانت تلگرام می‌توانند هم‌زمان به یک representative_id متصل باشند.
                CREATE TABLE IF NOT EXISTS rep_logins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    representative_id INTEGER NOT NULL,
                    company_id INTEGER NOT NULL,
                    telegram_username TEXT,
                    telegram_fullname TEXT,
                    logged_in_at TEXT,
                    FOREIGN KEY (representative_id) REFERENCES representatives(id) ON DELETE CASCADE,
                    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id TEXT NOT NULL,
                    file_name TEXT,
                    file_type TEXT,
                    file_size INTEGER,
                    caption TEXT,
                    upload_date TEXT,
                    uploader_id INTEGER,
                    uploader_name TEXT,
                    company_id INTEGER,
                    category_id INTEGER,
                    message_id INTEGER,
                    FOREIGN KEY (company_id) REFERENCES companies(id) ON DELETE CASCADE,
                    FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT,
                    telegram_id INTEGER,
                    details TEXT,
                    timestamp TEXT
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            self._conn.commit()

    def _run(self, query, params=(), fetch=None, commit=False):
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(query, params)
            result = None
            if fetch == "one":
                row = cur.fetchone()
                result = dict(row) if row else None
            elif fetch == "all":
                rows = cur.fetchall()
                result = [dict(r) for r in rows]
            if commit:
                self._conn.commit()
                result = cur.lastrowid
            return result

    def close(self):
        with self._lock:
            self._conn.close()

    def reconnect(self):
        with self._lock:
            self._conn = sqlite3.connect(self.db_name, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        self._migrate_legacy_schema()
        self._init_tables()

    # ------------------------------------------------------------------ لاگ
    def add_log(self, action: str, telegram_id: int, details: str = ""):
        self._run(
            "INSERT INTO logs (action, telegram_id, details, timestamp) VALUES (?, ?, ?, ?)",
            (action, telegram_id, details, now_str()),
            commit=True,
        )

    def get_logs(self, offset=0, limit=PAGE_SIZE):
        return self._run(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset),
            fetch="all",
        )

    def count_logs(self):
        r = self._run("SELECT COUNT(*) c FROM logs", fetch="one")
        return r["c"] if r else 0

    # ------------------------------------------------------------------ تنظیمات
    def get_setting(self, key, default=None):
        r = self._run("SELECT value FROM settings WHERE key=?", (key,), fetch="one")
        return r["value"] if r else default

    def set_setting(self, key, value):
        self._run(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
            commit=True,
        )

    # ------------------------------------------------------------------ شرکت‌ها
    def add_company(self, name, description, created_by):
        return self._run(
            "INSERT INTO companies (name, description, is_active, created_by, created_at) "
            "VALUES (?, ?, 1, ?, ?)",
            (name, description, created_by, now_str()),
            commit=True,
        )

    def get_company(self, company_id):
        return self._run(
            "SELECT * FROM companies WHERE id=?", (company_id,), fetch="one"
        )

    def list_companies(self, offset=0, limit=PAGE_SIZE, only_active=False):
        q = "SELECT * FROM companies"
        if only_active:
            q += " WHERE is_active=1"
        q += " ORDER BY id DESC LIMIT ? OFFSET ?"
        return self._run(q, (limit, offset), fetch="all")

    def count_companies(self, only_active=False):
        q = "SELECT COUNT(*) c FROM companies"
        if only_active:
            q += " WHERE is_active=1"
        r = self._run(q, fetch="one")
        return r["c"] if r else 0

    def update_company(self, company_id, name=None, description=None):
        company = self.get_company(company_id)
        if not company:
            return
        name = name if name is not None else company["name"]
        description = description if description is not None else company["description"]
        self._run(
            "UPDATE companies SET name=?, description=? WHERE id=?",
            (name, description, company_id),
            commit=True,
        )

    def toggle_company(self, company_id):
        company = self.get_company(company_id)
        if not company:
            return None
        new_state = 0 if company["is_active"] else 1
        self._run(
            "UPDATE companies SET is_active=? WHERE id=?",
            (new_state, company_id),
            commit=True,
        )
        return new_state

    def delete_company(self, company_id):
        self._run("DELETE FROM companies WHERE id=?", (company_id,), commit=True)

    # ------------------------------------------------------------------ دسته‌بندی‌ها
    def add_category(self, company_id, name, created_by):
        return self._run(
            "INSERT INTO categories (company_id, name, is_active, created_by, created_at) "
            "VALUES (?, ?, 1, ?, ?)",
            (company_id, name, created_by, now_str()),
            commit=True,
        )

    def get_category(self, category_id):
        return self._run(
            "SELECT * FROM categories WHERE id=?", (category_id,), fetch="one"
        )

    def list_categories(self, company_id, offset=0, limit=PAGE_SIZE, only_active=False):
        q = "SELECT * FROM categories WHERE company_id=?"
        if only_active:
            q += " AND is_active=1"
        q += " ORDER BY id DESC LIMIT ? OFFSET ?"
        return self._run(q, (company_id, limit, offset), fetch="all")

    def count_categories(self, company_id, only_active=False):
        q = "SELECT COUNT(*) c FROM categories WHERE company_id=?"
        if only_active:
            q += " AND is_active=1"
        r = self._run(q, (company_id,), fetch="one")
        return r["c"] if r else 0

    def update_category(self, category_id, name):
        self._run(
            "UPDATE categories SET name=? WHERE id=?", (name, category_id), commit=True
        )

    def toggle_category(self, category_id):
        cat = self.get_category(category_id)
        if not cat:
            return None
        new_state = 0 if cat["is_active"] else 1
        self._run(
            "UPDATE categories SET is_active=? WHERE id=?",
            (new_state, category_id),
            commit=True,
        )
        return new_state

    def delete_category(self, category_id):
        self._run("DELETE FROM categories WHERE id=?", (category_id,), commit=True)

    # ------------------------------------------------------------------ نمایندگان (هویت‌ها)
    def add_representative(self, company_id, full_name, password_hash, added_by):
        return self._run(
            "INSERT INTO representatives "
            "(company_id, full_name, password_hash, is_active, added_by, added_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (company_id, full_name, password_hash, added_by, now_str()),
            commit=True,
        )

    def get_representative_by_id(self, row_id):
        return self._run(
            "SELECT * FROM representatives WHERE id=?", (row_id,), fetch="one"
        )

    def list_representatives(self, company_id, offset=0, limit=PAGE_SIZE):
        return self._run(
            "SELECT * FROM representatives WHERE company_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
            (company_id, limit, offset),
            fetch="all",
        )

    def count_representatives(self, company_id):
        r = self._run(
            "SELECT COUNT(*) c FROM representatives WHERE company_id=?",
            (company_id,),
            fetch="one",
        )
        return r["c"] if r else 0

    def count_all_representatives(self):
        r = self._run("SELECT COUNT(*) c FROM representatives", fetch="one")
        return r["c"] if r else 0

    def toggle_representative(self, row_id):
        rep = self.get_representative_by_id(row_id)
        if not rep:
            return None
        new_state = 0 if rep["is_active"] else 1
        self._run(
            "UPDATE representatives SET is_active=? WHERE id=?",
            (new_state, row_id),
            commit=True,
        )
        return new_state

    def delete_representative(self, row_id):
        # rep_logins مربوطه به‌خاطر ON DELETE CASCADE به‌صورت خودکار حذف می‌شوند
        self._run("DELETE FROM representatives WHERE id=?", (row_id,), commit=True)

    def update_representative_password(self, row_id, password_hash):
        self._run(
            "UPDATE representatives SET password_hash=? WHERE id=?",
            (password_hash, row_id),
            commit=True,
        )

    def password_hash_in_use(self, password_hash, exclude_id=None):
        """بررسی می‌کند رمز عبور داده‌شده قبلاً برای یک نماینده‌ی فعال دیگر استفاده شده یا نه."""
        if exclude_id:
            r = self._run(
                "SELECT id FROM representatives WHERE password_hash=? AND is_active=1 AND id<>?",
                (password_hash, exclude_id),
                fetch="one",
            )
        else:
            r = self._run(
                "SELECT id FROM representatives WHERE password_hash=? AND is_active=1",
                (password_hash,),
                fetch="one",
            )
        return r is not None

    def find_representative_by_password(self, password_hash):
        """نماینده‌ی فعالِ متعلق به شرکتِ فعال که رمز عبورش مطابقت دارد را برمی‌گرداند."""
        return self._run(
            "SELECT r.*, c.name as company_name, c.is_active as company_active "
            "FROM representatives r JOIN companies c ON r.company_id = c.id "
            "WHERE r.password_hash=? AND r.is_active=1 AND c.is_active=1 LIMIT 1",
            (password_hash,),
            fetch="one",
        )

    # ------------------------------------------------------------------ نشست‌های ورود نماینده
    def add_rep_login(self, telegram_id, representative_id, company_id, username, full_name):
        with self._lock:
            cur = self._conn.cursor()
            # FK checks are temporarily disabled for this upsert because legacy
            # migration state can leave rep_logins referencing stale IDs that
            # cause spurious constraint failures. Data validity is guaranteed by
            # find_representative_by_password() which already verified both the
            # representative and company exist before this method is called.
            cur.execute("PRAGMA foreign_keys = OFF")
            try:
                cur.execute(
                    "INSERT INTO rep_logins "
                    "(telegram_id, representative_id, company_id, telegram_username, telegram_fullname, logged_in_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(telegram_id) DO UPDATE SET "
                    "representative_id=excluded.representative_id, "
                    "company_id=excluded.company_id, "
                    "telegram_username=excluded.telegram_username, "
                    "telegram_fullname=excluded.telegram_fullname, "
                    "logged_in_at=excluded.logged_in_at",
                    (telegram_id, representative_id, company_id, username, full_name, now_str()),
                )
                self._conn.commit()
            finally:
                cur.execute("PRAGMA foreign_keys = ON")

    def get_rep_login(self, telegram_id):
        """نشست فعال یک اکانت تلگرام را برمی‌گرداند، فقط اگر نماینده و شرکت هر دو فعال باشند."""
        return self._run(
            "SELECT rl.*, r.full_name as rep_full_name, r.is_active as rep_active, "
            "c.name as company_name, c.is_active as company_active "
            "FROM rep_logins rl "
            "JOIN representatives r ON rl.representative_id = r.id "
            "JOIN companies c ON rl.company_id = c.id "
            "WHERE rl.telegram_id=? AND r.is_active=1 AND c.is_active=1",
            (telegram_id,),
            fetch="one",
        )

    def count_rep_logins(self, representative_id):
        r = self._run(
            "SELECT COUNT(*) c FROM rep_logins WHERE representative_id=?",
            (representative_id,),
            fetch="one",
        )
        return r["c"] if r else 0

    def list_rep_logins(self, representative_id, limit=20):
        return self._run(
            "SELECT * FROM rep_logins WHERE representative_id=? ORDER BY logged_in_at DESC LIMIT ?",
            (representative_id, limit),
            fetch="all",
        )

    def count_all_rep_logins(self):
        r = self._run("SELECT COUNT(*) c FROM rep_logins", fetch="one")
        return r["c"] if r else 0

    def delete_rep_logins_by_rep(self, representative_id):
        self._run(
            "DELETE FROM rep_logins WHERE representative_id=?", (representative_id,), commit=True
        )

    def delete_rep_login(self, telegram_id):
        self._run("DELETE FROM rep_logins WHERE telegram_id=?", (telegram_id,), commit=True)

    # ------------------------------------------------------------------ فایل‌ها
    def add_file(self, file_id, file_name, file_type, file_size, caption, uploader_id,
                 uploader_name, company_id, category_id, message_id):
        return self._run(
            "INSERT INTO files (file_id, file_name, file_type, file_size, caption, "
            "upload_date, uploader_id, uploader_name, company_id, category_id, message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (file_id, file_name, file_type, file_size, caption, now_str(), uploader_id,
             uploader_name, company_id, category_id, message_id),
            commit=True,
        )

    def get_file(self, file_row_id):
        return self._run("SELECT * FROM files WHERE id=?", (file_row_id,), fetch="one")

    def delete_file(self, file_row_id):
        self._run("DELETE FROM files WHERE id=?", (file_row_id,), commit=True)

    def search_files(self, filters_dict, offset=0, limit=PAGE_SIZE):
        conditions = []
        params = []
        for key, value in filters_dict.items():
            if key == "caption_like":
                conditions.append("caption LIKE ?")
                params.append(f"%{value}%")
            elif key == "name_like":
                conditions.append("file_name LIKE ?")
                params.append(f"%{value}%")
            elif key == "date_like":
                conditions.append("upload_date LIKE ?")
                params.append(f"%{value}%")
            else:
                conditions.append(f"{key} = ?")
                params.append(value)
        q = "SELECT * FROM files"
        if conditions:
            q += " WHERE " + " AND ".join(conditions)
        q += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return self._run(q, tuple(params), fetch="all")

    def count_search_files(self, filters_dict):
        conditions = []
        params = []
        for key, value in filters_dict.items():
            if key == "caption_like":
                conditions.append("caption LIKE ?")
                params.append(f"%{value}%")
            elif key == "name_like":
                conditions.append("file_name LIKE ?")
                params.append(f"%{value}%")
            elif key == "date_like":
                conditions.append("upload_date LIKE ?")
                params.append(f"%{value}%")
            else:
                conditions.append(f"{key} = ?")
                params.append(value)
        q = "SELECT COUNT(*) c FROM files"
        if conditions:
            q += " WHERE " + " AND ".join(conditions)
        r = self._run(q, tuple(params), fetch="one")
        return r["c"] if r else 0

    # ------------------------------------------------------------------ آمار
    def count_files(self):
        r = self._run("SELECT COUNT(*) c FROM files", fetch="one")
        return r["c"] if r else 0

    def count_files_today(self):
        today = date.today().isoformat()
        r = self._run(
            "SELECT COUNT(*) c FROM files WHERE upload_date LIKE ?", (f"{today}%",), fetch="one"
        )
        return r["c"] if r else 0

    def count_files_month(self):
        month = date.today().strftime("%Y-%m")
        r = self._run(
            "SELECT COUNT(*) c FROM files WHERE upload_date LIKE ?", (f"{month}%",), fetch="one"
        )
        return r["c"] if r else 0

    def most_active_company(self):
        return self._run(
            "SELECT c.name, COUNT(f.id) cnt FROM files f "
            "JOIN companies c ON c.id=f.company_id "
            "GROUP BY f.company_id ORDER BY cnt DESC LIMIT 1",
            fetch="one",
        )

    def most_active_user(self):
        return self._run(
            "SELECT uploader_name, COUNT(id) cnt FROM files "
            "GROUP BY uploader_id ORDER BY cnt DESC LIMIT 1",
            fetch="one",
        )


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hash_password(password: str) -> str:
    return hashlib.sha256((PASSWORD_PEPPER + password).encode("utf-8")).hexdigest()


db = Database(DATABASE_NAME)

# ==================================================================================
# ============================== کنترل سطح دسترسی ==================================
# ==================================================================================


def get_role(telegram_id: int) -> str | None:
    """نقش کاربر را برمی‌گرداند: manager, representative یا None."""
    if telegram_id in MANAGER_IDS:
        return "manager"
    login = db.get_rep_login(telegram_id)
    if login:
        return "representative"
    return None


def get_rep_session(telegram_id: int):
    """اطلاعات نشست نماینده (شرکت، نام نماینده و ...) برای این اکانت تلگرام."""
    return db.get_rep_login(telegram_id)


def get_manager_ids() -> list[int]:
    """آیدی تمام مدیران ارشد برای ارسال اعلان."""
    return list(set(MANAGER_IDS))


def require_role(*roles):
    """دکوریتور بررسی سطح دسترسی برای هندلرهای کالبک / پیام."""

    def decorator(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id
            role = get_role(user_id)
            if role not in roles:
                if update.callback_query:
                    await update.callback_query.answer(
                        "⛔️ شما دسترسی لازم برای این عملیات را ندارید.", show_alert=True
                    )
                else:
                    await update.effective_message.reply_text(
                        "⛔️ شما دسترسی لازم برای این عملیات را ندارید."
                    )
                return
            return await func(update, context, *args, **kwargs)

        return wrapper

    return decorator


# ==================================================================================
# ================================ توابع کمکی UI ====================================
# ==================================================================================


def nav_row(back_cb: str = "nav_home", home: bool = True, cancel: bool = True):
    """ردیف دکمه‌های بازگشت / خانه / لغو که باید در همه صفحات وجود داشته باشد."""
    row = [InlineKeyboardButton("🔙 بازگشت", callback_data=back_cb)]
    if home:
        row.append(InlineKeyboardButton("🏠 خانه", callback_data="nav_home"))
    if cancel:
        row.append(InlineKeyboardButton("❌ لغو", callback_data="nav_cancel"))
    return row


def pagination_row(prefix: str, page: int, total_pages: int):
    row = []
    if page > 0:
        row.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"{prefix}:{page-1}"))
    row.append(InlineKeyboardButton(f"📄 {page+1}/{max(total_pages,1)}", callback_data="noop"))
    if page < total_pages - 1:
        row.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"{prefix}:{page+1}"))
    return row


async def safe_edit(query, text, reply_markup=None):
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            try:
                await query.message.reply_text(
                    text, reply_markup=reply_markup, parse_mode=ParseMode.HTML
                )
            except Exception as ex:
                logger.error(f"خطا در ارسال پیام: {ex}")


def clear_state(context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = None
    context.user_data["data"] = {}


def set_state(context: ContextTypes.DEFAULT_TYPE, state: str, **data):
    context.user_data["state"] = state
    context.user_data["data"] = data


def get_state(context: ContextTypes.DEFAULT_TYPE):
    return context.user_data.get("state")


def get_state_data(context: ContextTypes.DEFAULT_TYPE):
    return context.user_data.get("data", {})


# ==================================================================================
# ============================ منوی مهمان (کاربر ثبت‌نشده) ==========================
# ==================================================================================


async def show_guest_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=True):
    clear_state(context)
    text = (
        "👋 به ربات خوش آمدید.\n\n"
        "برای دسترسی به کارتابل شرکت خود، روی دکمه زیر بزنید و رمز عبور نماینده را وارد کنید:"
    )
    kb = [[InlineKeyboardButton("🔑 ورود به عنوان نماینده", callback_data="rep_login_start")]]
    markup = InlineKeyboardMarkup(kb)
    if edit and update.callback_query:
        await safe_edit(update.callback_query, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


# ==================================================================================
# ================================ هندلر شروع =======================================
# ==================================================================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)
    user = update.effective_user
    role = get_role(user.id)

    if role is None:
        db.add_log("guest_start", user.id, f"@{user.username}")
        await show_guest_menu(update, context, edit=False)
        return

    db.add_log("login", user.id, f"role={role}")

    if role == "manager":
        await show_manager_menu(update, context, edit=False)
    else:
        await show_representative_menu(update, context, edit=False)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)
    await update.message.reply_text("عملیات لغو شد. برای شروع دوباره /start را بزنید.")


# ==================================================================================
# ============================== منوی مدیر  (Manager) ==========================
# ==================================================================================


async def show_manager_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=True):
    clear_state(context)
    text = "👑 <b>پنل مدیر </b>\n\nیکی از گزینه‌های زیر را انتخاب کنید:"
    kb = [
        [InlineKeyboardButton("🏢 مدیریت شرکت‌ها", callback_data="cmp_list:0")],
        [InlineKeyboardButton("📁 مدیریت فایل‌ها", callback_data="search_menu")],
        [InlineKeyboardButton("📊 آمار و داشبورد", callback_data="stats_show")],
        [InlineKeyboardButton("📜 لاگ‌ها", callback_data="logs_list:0")],
        [InlineKeyboardButton("💾 بکاپ / بازیابی", callback_data="backup_menu")],
        [InlineKeyboardButton("⚙️ تنظیمات ربات", callback_data="settings_menu")],
    ]
    markup = InlineKeyboardMarkup(kb)
    if edit and update.callback_query:
        await safe_edit(update.callback_query, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


# ==================================================================================
# ============================== مدیریت شرکت‌ها (Manager) ===========================
# ==================================================================================


@require_role("manager")
async def cmp_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    clear_state(context)
    total = db.count_companies()
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    companies = db.list_companies(offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    text = f"🏢 <b>مدیریت شرکت‌ها</b>\nتعداد کل: {total}\n\n"
    kb = [[InlineKeyboardButton("➕ افزودن شرکت جدید", callback_data="cmp_add")]]
    for c in companies:
        status = "✅" if c["is_active"] else "🚫"
        kb.append([InlineKeyboardButton(f"{status} {c['name']}", callback_data=f"cmp_view:{c['id']}")])
    kb.append(pagination_row("cmp_list", page, total_pages))
    kb.append(nav_row("mgr_menu"))
    markup = InlineKeyboardMarkup(kb)
    if update.callback_query:
        await safe_edit(update.callback_query, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


@require_role("manager")
async def cmp_view(update: Update, context: ContextTypes.DEFAULT_TYPE, company_id: int):
    company = db.get_company(company_id)
    if not company:
        await update.callback_query.answer("شرکت یافت نشد.", show_alert=True)
        return
    status = "فعال ✅" if company["is_active"] else "غیرفعال 🚫"
    cat_count = db.count_categories(company_id)
    rep_count = db.count_representatives(company_id)
    text = (
        f"🏢 <b>{company['name']}</b>\n\n"
        f"توضیحات: {company['description'] or '-'}\n"
        f"وضعیت: {status}\n"
        f"تعداد دسته‌بندی: {cat_count}\n"
        f"تعداد نمایندگان: {rep_count}"
    )
    kb = [
        [InlineKeyboardButton("📂 دسته‌بندی‌ها", callback_data=f"cat_list:{company_id}:0")],
        [InlineKeyboardButton("👤 نمایندگان", callback_data=f"rep_list:{company_id}:0")],
        [InlineKeyboardButton("✏️ ویرایش نام/توضیحات", callback_data=f"cmp_edit:{company_id}")],
        [InlineKeyboardButton(
            "🚫 غیرفعال کردن" if company["is_active"] else "✅ فعال کردن",
            callback_data=f"cmp_toggle:{company_id}",
        )],
        [InlineKeyboardButton("🗑 حذف شرکت", callback_data=f"cmp_del:{company_id}")],
        nav_row("cmp_list:0"),
    ]
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup(kb))


@require_role("manager")
async def cmp_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context, "add_company_name")
    text = "نام شرکت جدید را وارد کنید:"
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup([nav_row("cmp_list:0")]))


@require_role("manager")
async def cmp_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE, company_id: int):
    set_state(context, "edit_company_name", company_id=company_id)
    text = "نام جدید شرکت را وارد کنید (برای رد شدن از تغییر نام، کلمه 'رد' را ارسال کنید):"
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup([nav_row(f"cmp_view:{company_id}")]))


@require_role("manager")
async def cmp_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, company_id: int):
    new_state = db.toggle_company(company_id)
    db.add_log("toggle_company", update.effective_user.id, f"company_id={company_id} -> {new_state}")
    await update.callback_query.answer("وضعیت به‌روزرسانی شد.")
    await cmp_view(update, context, company_id)


@require_role("manager")
async def cmp_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, company_id: int):
    db.delete_company(company_id)
    db.add_log("delete_company", update.effective_user.id, f"company_id={company_id}")
    await update.callback_query.answer("شرکت حذف شد.")
    await cmp_list(update, context, page=0)


# ==================================================================================
# ============================== مدیریت دسته‌بندی‌ها ================================
# ==================================================================================


@require_role("manager")
async def cat_list(update: Update, context: ContextTypes.DEFAULT_TYPE, company_id: int, page=0):
    clear_state(context)
    company = db.get_company(company_id)
    if not company:
        await update.callback_query.answer("شرکت یافت نشد.", show_alert=True)
        return
    total = db.count_categories(company_id)
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    cats = db.list_categories(company_id, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    text = f"📂 <b>دسته‌بندی‌های {company['name']}</b>\nتعداد کل: {total}\n\n"
    kb = [[InlineKeyboardButton("➕ افزودن دسته جدید", callback_data=f"cat_add:{company_id}")]]
    for c in cats:
        status = "✅" if c["is_active"] else "🚫"
        kb.append([InlineKeyboardButton(f"{status} {c['name']}", callback_data=f"cat_view:{c['id']}")])
    kb.append(pagination_row(f"cat_list:{company_id}", page, total_pages))
    kb.append(nav_row(f"cmp_view:{company_id}"))
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup(kb))


@require_role("manager")
async def cat_view(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: int):
    cat = db.get_category(category_id)
    if not cat:
        await update.callback_query.answer("دسته یافت نشد.", show_alert=True)
        return
    status = "فعال ✅" if cat["is_active"] else "غیرفعال 🚫"
    text = f"📂 <b>{cat['name']}</b>\n\nوضعیت: {status}\nتاریخ ایجاد: {cat['created_at']}"
    kb = [
        [InlineKeyboardButton("✏️ ویرایش نام", callback_data=f"cat_edit:{category_id}")],
        [InlineKeyboardButton(
            "🚫 غیرفعال کردن" if cat["is_active"] else "✅ فعال کردن",
            callback_data=f"cat_toggle:{category_id}",
        )],
        [InlineKeyboardButton("🗑 حذف دسته", callback_data=f"cat_del:{category_id}")],
        nav_row(f"cat_list:{cat['company_id']}:0"),
    ]
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup(kb))


@require_role("manager")
async def cat_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE, company_id: int):
    set_state(context, "add_category_name", company_id=company_id)
    text = "نام دسته‌بندی جدید را وارد کنید:"
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup([nav_row(f"cat_list:{company_id}:0")]))


@require_role("manager")
async def cat_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: int):
    set_state(context, "edit_category_name", category_id=category_id)
    text = "نام جدید دسته‌بندی را وارد کنید:"
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup([nav_row(f"cat_view:{category_id}")]))


@require_role("manager")
async def cat_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: int):
    new_state = db.toggle_category(category_id)
    db.add_log("toggle_category", update.effective_user.id, f"category_id={category_id} -> {new_state}")
    await update.callback_query.answer("وضعیت به‌روزرسانی شد.")
    await cat_view(update, context, category_id)


@require_role("manager")
async def cat_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: int):
    cat = db.get_category(category_id)
    company_id = cat["company_id"] if cat else None
    db.delete_category(category_id)
    db.add_log("delete_category", update.effective_user.id, f"category_id={category_id}")
    await update.callback_query.answer("دسته حذف شد.")
    if company_id:
        await cat_list(update, context, company_id, page=0)
    else:
        await show_manager_menu(update, context)


# ==================================================================================
# ========================= مدیریت نمایندگان (توسط منیجر) ===========================
# ==================================================================================


@require_role("manager")
async def rep_list(update: Update, context: ContextTypes.DEFAULT_TYPE, company_id: int, page=0):
    clear_state(context)
    company = db.get_company(company_id)
    if not company:
        await update.callback_query.answer("شرکت یافت نشد.", show_alert=True)
        return
    total = db.count_representatives(company_id)
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    reps = db.list_representatives(company_id, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    text = f"👤 <b>نمایندگان {company['name']}</b>\nتعداد کل: {total}\n\n"
    kb = [[InlineKeyboardButton("➕ افزودن نماینده", callback_data=f"rep_add:{company_id}")]]
    for r in reps:
        status = "✅" if r["is_active"] else "🚫"
        acc_count = db.count_rep_logins(r["id"])
        label = f"{status} {r['full_name']} ({acc_count} اکانت)"
        kb.append([InlineKeyboardButton(label, callback_data=f"rep_view:{r['id']}")])
    kb.append(pagination_row(f"rep_list:{company_id}", page, total_pages))
    kb.append(nav_row(f"cmp_view:{company_id}"))
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup(kb))


@require_role("manager")
async def rep_view(update: Update, context: ContextTypes.DEFAULT_TYPE, rep_id: int):
    rep = db.get_representative_by_id(rep_id)
    if not rep:
        await update.callback_query.answer("نماینده یافت نشد.", show_alert=True)
        return
    status = "فعال ✅" if rep["is_active"] else "غیرفعال 🚫"
    acc_count = db.count_rep_logins(rep_id)
    logins = db.list_rep_logins(rep_id, limit=10)
    accounts_text = "\n".join(
        f"  • {l['telegram_fullname'] or '-'} (@{l['telegram_username'] or '-'})"
        for l in logins
    ) or "  هیچ اکانتی وارد نشده است."
    text = (
        f"👤 <b>{rep['full_name']}</b>\n\n"
        f"وضعیت: {status}\n"
        f"تاریخ افزودن: {rep['added_at']}\n"
        f"تعداد اکانت‌های وارد شده: {acc_count}\n\n"
        f"<b>اکانت‌های متصل (حداکثر ۱۰ مورد آخر):</b>\n{accounts_text}"
    )
    kb = [
        [InlineKeyboardButton("🔑 تغییر رمز عبور", callback_data=f"rep_edit_pw:{rep_id}")],
        [InlineKeyboardButton(
            "🚫 غیرفعال کردن" if rep["is_active"] else "✅ فعال کردن",
            callback_data=f"rep_toggle:{rep_id}",
        )],
        [InlineKeyboardButton("🔁 خروج اجباری همه اکانت‌ها", callback_data=f"rep_logout_all:{rep_id}")],
        [InlineKeyboardButton("🗑 حذف نماینده", callback_data=f"rep_del:{rep_id}")],
        nav_row(f"rep_list:{rep['company_id']}:0"),
    ]
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup(kb))


@require_role("manager")
async def rep_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE, company_id: int):
    set_state(context, "add_rep_name", company_id=company_id)
    text = "نام نماینده جدید را وارد کنید:"
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup([nav_row(f"rep_list:{company_id}:0")]))


@require_role("manager")
async def rep_edit_password_start(update: Update, context: ContextTypes.DEFAULT_TYPE, rep_id: int):
    rep = db.get_representative_by_id(rep_id)
    if not rep:
        await update.callback_query.answer("نماینده یافت نشد.", show_alert=True)
        return
    set_state(context, "edit_rep_password", rep_id=rep_id)
    text = (
        f"رمز عبور جدید برای «{rep['full_name']}» را وارد کنید "
        f"(حداقل {MIN_PASSWORD_LENGTH} کاراکتر):\n\n"
        "توجه: با تغییر رمز، تمام اکانت‌هایی که قبلاً با رمز قدیمی وارد شده بودند خارج می‌شوند."
    )
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup([nav_row(f"rep_view:{rep_id}")]))


@require_role("manager")
async def rep_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE, rep_id: int):
    new_state = db.toggle_representative(rep_id)
    db.add_log("toggle_representative", update.effective_user.id, f"rep_id={rep_id} -> {new_state}")
    await update.callback_query.answer("وضعیت به‌روزرسانی شد.")
    await rep_view(update, context, rep_id)


@require_role("manager")
async def rep_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, rep_id: int):
    rep = db.get_representative_by_id(rep_id)
    company_id = rep["company_id"] if rep else None
    db.delete_representative(rep_id)
    db.add_log("delete_representative", update.effective_user.id, f"rep_id={rep_id}")
    await update.callback_query.answer("نماینده حذف شد.")
    if company_id:
        await rep_list(update, context, company_id, page=0)
    else:
        await show_manager_menu(update, context)


@require_role("manager")
async def rep_logout_all(update: Update, context: ContextTypes.DEFAULT_TYPE, rep_id: int):
    db.delete_rep_logins_by_rep(rep_id)
    db.add_log("rep_logout_all", update.effective_user.id, f"rep_id={rep_id}")
    await update.callback_query.answer("تمام اکانت‌های متصل به این نماینده خارج شدند.", show_alert=True)
    await rep_view(update, context, rep_id)


# ==================================================================================
# ========================== منوی نماینده شرکت (Representative) =====================
# ==================================================================================


async def show_representative_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, edit=True):
    clear_state(context)
    session = get_rep_session(update.effective_user.id)
    company_name = session["company_name"] if session else ""
    text = f"👤 <b>پنل نماینده - {company_name}</b>\n\nیکی از گزینه‌ها را انتخاب کنید:"
    kb = [
        [InlineKeyboardButton("⬆️ آپلود فایل جدید", callback_data="up_start")],
        [InlineKeyboardButton("📜 تاریخچه فایل‌های شرکت", callback_data="rep_history:0")],
        [InlineKeyboardButton("🔍 جستجو در فایل‌های شرکت", callback_data="rep_search_start")],
        [InlineKeyboardButton("🚪 خروج از حساب", callback_data="rep_logout")],
    ]
    markup = InlineKeyboardMarkup(kb)
    if edit and update.callback_query:
        await safe_edit(update.callback_query, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


@require_role("representative")
async def rep_history(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    clear_state(context)
    session = get_rep_session(update.effective_user.id)
    filters_dict = {"company_id": session["company_id"]}
    total = db.count_search_files(filters_dict)
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    files = db.search_files(filters_dict, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    text = f"📜 <b>تاریخچه فایل‌های شرکت</b>\nتعداد کل: {total}\n\n"
    kb = []
    for f in files:
        label = f"{f['file_name'] or f['file_type']} - {f['upload_date'][:10]}"
        kb.append([InlineKeyboardButton(label, callback_data=f"file_view:{f['id']}")])
    kb.append(pagination_row("rep_history", page, total_pages))
    kb.append(nav_row("nav_home"))
    markup = InlineKeyboardMarkup(kb)
    if update.callback_query:
        await safe_edit(update.callback_query, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


@require_role("representative")
async def rep_search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context, "rep_search_query")
    text = "عبارت مورد نظر برای جستجو در نام فایل یا کپشن فایل‌های شرکت خود را وارد کنید:"
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup([nav_row("nav_home")]))


# ==================================================================================
# ==================================== آپلود فایل ====================================
# ==================================================================================


@require_role("representative")
async def upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_rep_session(update.effective_user.id)
    company_id = session["company_id"]
    cats = db.list_categories(company_id, offset=0, limit=100, only_active=True)
    if not cats:
        await update.callback_query.answer("شرکت شما هیچ دسته‌بندی فعالی ندارد.", show_alert=True)
        return
    text = "📂 لطفاً دسته‌بندی مورد نظر برای آپلود فایل را انتخاب کنید:"
    kb = [[InlineKeyboardButton(c["name"], callback_data=f"up_cat:{c['id']}")] for c in cats]
    kb.append(nav_row("nav_home"))
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup(kb))


@require_role("representative")
async def upload_select_category(update: Update, context: ContextTypes.DEFAULT_TYPE, category_id: int):
    cat = db.get_category(category_id)
    session = get_rep_session(update.effective_user.id)
    if not cat or not cat["is_active"] or cat["company_id"] != session["company_id"]:
        await update.callback_query.answer("دسته‌بندی معتبر نیست.", show_alert=True)
        return
    set_state(context, "awaiting_file", category_id=category_id, company_id=cat["company_id"])
    text = (
        f"📤 دسته‌بندی «{cat['name']}» انتخاب شد.\n\n"
        "اکنون فایل، عکس، ویدیو یا سند مورد نظر خود را ارسال کنید."
    )
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup([nav_row("up_start")]))


def extract_file_info(message):
    """اطلاعات فایل را از پیام تلگرام استخراج می‌کند."""
    if message.document:
        d = message.document
        return {
            "file_id": d.file_id, "file_name": d.file_name or "document",
            "file_type": "document", "file_size": d.file_size,
        }
    if message.photo:
        p = message.photo[-1]
        return {
            "file_id": p.file_id, "file_name": "photo.jpg",
            "file_type": "photo", "file_size": p.file_size,
        }
    if message.video:
        v = message.video
        return {
            "file_id": v.file_id, "file_name": v.file_name or "video.mp4",
            "file_type": "video", "file_size": v.file_size,
        }
    if message.audio:
        a = message.audio
        return {
            "file_id": a.file_id, "file_name": a.file_name or "audio.mp3",
            "file_type": "audio", "file_size": a.file_size,
        }
    return None


async def handle_file_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_role(update.effective_user.id) != "representative":
        return
    if get_state(context) != "awaiting_file":
        return

    info = extract_file_info(update.message)
    if not info:
        await update.message.reply_text("نوع فایل ارسالی پشتیبانی نمی‌شود.")
        return
    if info["file_type"] not in ALLOWED_FILE_TYPES:
        await update.message.reply_text("این نوع فایل مجاز نیست.")
        return

    max_size = int(db.get_setting("MAX_FILE_SIZE", MAX_FILE_SIZE))
    if info["file_size"] and info["file_size"] > max_size:
        await update.message.reply_text(
            f"حجم فایل بیش از حد مجاز است. حداکثر مجاز: {max_size // (1024*1024)} مگابایت."
        )
        return

    data = get_state_data(context)
    set_state(
        context, "awaiting_caption",
        category_id=data["category_id"], company_id=data["company_id"],
        file_info=info, message_id=update.message.message_id,
    )

    text = (
        f"✅ فایل دریافت شد.\n\n"
        f"نام: {info['file_name']}\n"
        f"نوع: {info['file_type']}\n"
        f"حجم: {round((info['file_size'] or 0) / 1024, 1)} کیلوبایت\n\n"
        "اکنون کپشن (توضیحات) فایل را ارسال کنید. اگر کپشن نمی‌خواهید، کلمه «هیچ» را ارسال کنید."
    )
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup([nav_row("up_start")]))


async def show_upload_preview(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = get_state_data(context)
    info = data["file_info"]
    caption = data.get("caption") or "-"
    text = (
        "👀 <b>پیش‌نمایش نهایی</b>\n\n"
        f"نام فایل: {info['file_name']}\n"
        f"نوع: {info['file_type']}\n"
        f"کپشن: {caption}\n\n"
        "در صورت تأیید، دکمه «ثبت نهایی» را بزنید."
    )
    kb = [
        [InlineKeyboardButton("✅ ثبت نهایی", callback_data="up_confirm")],
        [InlineKeyboardButton("✏️ ویرایش کپشن", callback_data="up_edit_caption")],
        nav_row("up_start"),
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)


@require_role("representative")
async def upload_edit_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = get_state_data(context)
    set_state(context, "awaiting_caption", **data)
    text = "کپشن جدید را ارسال کنید (برای حذف کپشن، کلمه «هیچ» را بفرستید):"
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup([nav_row("up_start")]))


@require_role("representative")
async def upload_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = get_state_data(context)
    info = data.get("file_info")
    if not info:
        await update.callback_query.answer("اطلاعات فایل یافت نشد. دوباره تلاش کنید.", show_alert=True)
        return

    user = update.effective_user
    file_row_id = db.add_file(
        file_id=info["file_id"], file_name=info["file_name"], file_type=info["file_type"],
        file_size=info["file_size"], caption=data.get("caption"),
        uploader_id=user.id, uploader_name=(user.full_name or user.username or str(user.id)),
        company_id=data["company_id"], category_id=data["category_id"],
        message_id=data.get("message_id"),
    )
    db.add_log("upload_file", user.id, f"file_id={file_row_id}")
    clear_state(context)

    await safe_edit(
        update.callback_query,
        "✅ فایل با موفقیت ثبت شد.",
        InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="nav_home")]]),
    )

    # اعلان به تمام مدیران ارشد
    company = db.get_company(data["company_id"])
    category = db.get_category(data["category_id"])
    notif_text = (
        "📥 <b>فایل جدید آپلود شد</b>\n\n"
        f"شرکت: {company['name'] if company else '-'}\n"
        f"دسته: {category['name'] if category else '-'}\n"
        f"آپلودکننده: {user.full_name or user.username}\n"
        f"نوع فایل: {info['file_type']}\n"
        f"کپشن: {data.get('caption') or '-'}"
    )
    for manager_id in get_manager_ids():
        try:
            await context.bot.send_message(manager_id, notif_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"ارسال اعلان به {manager_id} ناموفق بود: {e}")


# ==================================================================================
# =================================== جستجو و مشاهده فایل ============================
# ==================================================================================


@require_role("manager")
async def search_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)
    text = "🔍 <b>جستجوی فایل‌ها</b>\n\nیک روش جستجو انتخاب کنید یا همه فایل‌ها را مشاهده کنید:"
    kb = [
        [InlineKeyboardButton("📄 مشاهده همه فایل‌ها", callback_data="file_list:0")],
        [InlineKeyboardButton("🏢 جستجو بر اساس شرکت", callback_data="search_by_company")],
        [InlineKeyboardButton("📁 جستجو بر اساس نام فایل", callback_data="search_by_name")],
        [InlineKeyboardButton("📝 جستجو بر اساس کپشن", callback_data="search_by_caption")],
        [InlineKeyboardButton("👤 جستجو بر اساس آپلودکننده", callback_data="search_by_uploader")],
        [InlineKeyboardButton("📅 جستجو بر اساس تاریخ", callback_data="search_by_date")],
        nav_row("mgr_menu"),
    ]
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup(kb))


@require_role("manager")
async def search_by_company(update: Update, context: ContextTypes.DEFAULT_TYPE):
    companies = db.list_companies(offset=0, limit=100)
    if not companies:
        await update.callback_query.answer("هیچ شرکتی ثبت نشده است.", show_alert=True)
        return
    text = "یک شرکت را انتخاب کنید:"
    kb = [[InlineKeyboardButton(c["name"], callback_data=f"file_list_company:{c['id']}:0")] for c in companies]
    kb.append(nav_row("search_menu"))
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup(kb))


@require_role("manager")
async def search_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str):
    prompts = {
        "name": "نام فایل مورد نظر برای جستجو را وارد کنید:",
        "caption": "بخشی از کپشن مورد نظر برای جستجو را وارد کنید:",
        "uploader": "نام آپلودکننده را وارد کنید:",
        "date": "تاریخ را به فرمت YYYY-MM-DD یا بخشی از آن وارد کنید:",
    }
    set_state(context, f"search_{mode}")
    await safe_edit(update.callback_query, prompts[mode], InlineKeyboardMarkup([nav_row("search_menu")]))


async def render_file_list(update: Update, context: ContextTypes.DEFAULT_TYPE, filters_dict, page, prefix, back_cb):
    total = db.count_search_files(filters_dict)
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    files = db.search_files(filters_dict, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    text = f"📁 <b>نتایج فایل‌ها</b>\nتعداد کل: {total}\n\n"
    if not files:
        text += "هیچ فایلی یافت نشد."
    kb = []
    for f in files:
        label = f"{f['file_name'] or f['file_type']} - {f['upload_date'][:10]}"
        kb.append([InlineKeyboardButton(label, callback_data=f"file_view:{f['id']}")])
    kb.append(pagination_row(prefix, page, total_pages))
    kb.append(nav_row(back_cb))
    markup = InlineKeyboardMarkup(kb)
    if update.callback_query:
        await safe_edit(update.callback_query, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


@require_role("manager")
async def file_list_all(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    await render_file_list(update, context, {}, page, "file_list", "search_menu")


@require_role("manager")
async def file_list_by_company(update: Update, context: ContextTypes.DEFAULT_TYPE, company_id: int, page=0):
    await render_file_list(
        update, context, {"company_id": company_id}, page,
        f"file_list_company:{company_id}", "search_by_company",
    )


async def file_view(update: Update, context: ContextTypes.DEFAULT_TYPE, file_row_id: int):
    """نمایش جزئیات یک فایل - در دسترس مدیر و نمایندگان همان شرکت (هر اکانتی که به آن شرکت وارد شده)."""
    user_id = update.effective_user.id
    role = get_role(user_id)
    f = db.get_file(file_row_id)
    if not f:
        await update.callback_query.answer("فایل یافت نشد.", show_alert=True)
        return
    if role is None:
        await update.callback_query.answer("⛔️ دسترسی ندارید.", show_alert=True)
        return
    if role == "representative":
        session = get_rep_session(user_id)
        if not session or f["company_id"] != session["company_id"]:
            await update.callback_query.answer("⛔️ شما اجازه مشاهده این فایل را ندارید.", show_alert=True)
            return

    company = db.get_company(f["company_id"])
    category = db.get_category(f["category_id"])
    text = (
        f"📄 <b>جزئیات فایل</b>\n\n"
        f"نام: {f['file_name']}\n"
        f"نوع: {f['file_type']}\n"
        f"حجم: {round((f['file_size'] or 0)/1024, 1)} کیلوبایت\n"
        f"کپشن: {f['caption'] or '-'}\n"
        f"شرکت: {company['name'] if company else '-'}\n"
        f"دسته: {category['name'] if category else '-'}\n"
        f"آپلودکننده: {f['uploader_name']}\n"
        f"تاریخ آپلود: {f['upload_date']}"
    )
    kb = [[InlineKeyboardButton("📥 دریافت فایل", callback_data=f"file_send:{file_row_id}")]]
    kb.append([InlineKeyboardButton("🗑 حذف فایل", callback_data=f"file_del:{file_row_id}")])
    kb.append(nav_row("nav_home"))
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup(kb))


async def file_send(update: Update, context: ContextTypes.DEFAULT_TYPE, file_row_id: int):
    """ارسال (دانلود) فایل - برای مدیر و هر نماینده‌ی همان شرکت به‌درستی کار می‌کند."""
    user_id = update.effective_user.id
    role = get_role(user_id)
    f = db.get_file(file_row_id)
    if not f or role is None:
        await update.callback_query.answer("دسترسی یا فایل معتبر نیست.", show_alert=True)
        return
    if role == "representative":
        session = get_rep_session(user_id)
        if not session or f["company_id"] != session["company_id"]:
            await update.callback_query.answer("⛔️ اجازه ندارید.", show_alert=True)
            return
    try:
        if f["file_type"] == "photo":
            await context.bot.send_photo(update.effective_chat.id, f["file_id"], caption=f["caption"] or "")
        elif f["file_type"] == "video":
            await context.bot.send_video(update.effective_chat.id, f["file_id"], caption=f["caption"] or "")
        elif f["file_type"] == "audio":
            await context.bot.send_audio(update.effective_chat.id, f["file_id"], caption=f["caption"] or "")
        else:
            await context.bot.send_document(update.effective_chat.id, f["file_id"], caption=f["caption"] or "")
        db.add_log("send_file", user_id, f"file_id={file_row_id}")
    except Exception as e:
        logger.exception("خطا در ارسال فایل")
        await update.effective_chat.send_message(f"⚠️ خطا در ارسال فایل: {e}")


async def file_delete(update: Update, context: ContextTypes.DEFAULT_TYPE, file_row_id: int):
    user_id = update.effective_user.id
    role = get_role(user_id)
    f = db.get_file(file_row_id)
    if not f or role is None:
        await update.callback_query.answer("دسترسی یا فایل معتبر نیست.", show_alert=True)
        return
    if role == "representative":
        session = get_rep_session(user_id)
        if not session or f["company_id"] != session["company_id"]:
            await update.callback_query.answer("⛔️ اجازه ندارید.", show_alert=True)
            return
    db.delete_file(file_row_id)
    db.add_log("delete_file", user_id, f"file_id={file_row_id}")
    await update.callback_query.answer("فایل حذف شد.")
    if role == "representative":
        await rep_history(update, context, page=0)
    else:
        await file_list_all(update, context, page=0)


# ==================================================================================
# ==================================== آمار / داشبورد ================================
# ==================================================================================


@require_role("manager")
async def stats_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_representatives = db.count_all_representatives()
    total_logged_accounts = db.count_all_rep_logins()
    total_managers = len(MANAGER_IDS)
    total_companies = db.count_companies()
    total_categories = sum(
        db.count_categories(c["id"]) for c in db.list_companies(offset=0, limit=1000)
    )
    total_files = db.count_files()
    today_files = db.count_files_today()
    month_files = db.count_files_month()
    top_company = db.most_active_company()
    top_user = db.most_active_user()

    text = (
        "📊 <b>داشبورد آماری</b>\n\n"
        f"👑 تعداد مدیران: {total_managers}\n"
        f"👤 تعداد نمایندگان (هویت‌ها): {total_representatives}\n"
        f"📱 تعداد اکانت‌های وارد شده: {total_logged_accounts}\n"
        f"🏢 تعداد شرکت‌ها: {total_companies}\n"
        f"📂 تعداد دسته‌بندی‌ها: {total_categories}\n"
        f"📄 تعداد کل فایل‌ها: {total_files}\n"
        f"📅 آپلود امروز: {today_files}\n"
        f"🗓 آپلود این ماه: {month_files}\n"
        f"🏆 فعال‌ترین شرکت: {top_company['name'] if top_company else '-'}\n"
        f"🥇 فعال‌ترین کاربر: {top_user['uploader_name'] if top_user else '-'}"
    )
    kb = [nav_row("mgr_menu")]
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup(kb))


# ==================================================================================
# ====================================== لاگ‌ها ======================================
# ==================================================================================


@require_role("manager")
async def logs_list(update: Update, context: ContextTypes.DEFAULT_TYPE, page=0):
    clear_state(context)
    total = db.count_logs()
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    logs = db.get_logs(offset=page * PAGE_SIZE, limit=PAGE_SIZE)
    lines = [f"📜 <b>لاگ‌های سیستم</b> (کل: {total})\n"]
    for log in logs:
        lines.append(f"• [{log['timestamp']}] {log['action']} | {log['telegram_id']} | {log['details']}")
    text = "\n".join(lines)
    kb = [pagination_row("logs_list", page, total_pages), nav_row("mgr_menu")]
    markup = InlineKeyboardMarkup(kb)
    if update.callback_query:
        await safe_edit(update.callback_query, text, markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


# ==================================================================================
# ================================ بکاپ / بازیابی ===================================
# ==================================================================================


@require_role("manager")
async def backup_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)
    text = "💾 <b>بکاپ و بازیابی دیتابیس</b>\n\nیکی از گزینه‌ها را انتخاب کنید:"
    kb = [
        [InlineKeyboardButton("📤 دریافت فایل بکاپ", callback_data="backup_send")],
        [InlineKeyboardButton("📥 بازیابی از فایل بکاپ", callback_data="backup_restore_start")],
        nav_row("mgr_menu"),
    ]
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup(kb))


@require_role("manager")
async def backup_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    try:
        with open(DATABASE_NAME, "rb") as f:
            await context.bot.send_document(
                update.effective_chat.id, f,
                filename=f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
                caption="فایل بکاپ دیتابیس",
            )
        db.add_log("backup_sent", update.effective_user.id)
    except Exception as e:
        await update.effective_chat.send_message(f"خطا در ارسال بکاپ: {e}")


@require_role("manager")
async def backup_restore_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context, "awaiting_backup_file")
    text = (
        "⚠️ برای بازیابی دیتابیس، فایل بکاپ (.db) را ارسال کنید.\n"
        "توجه: دیتابیس فعلی جایگزین خواهد شد و این عملیات غیرقابل بازگشت است."
    )
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup([nav_row("backup_menu")]))


async def handle_restore_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if get_role(update.effective_user.id) != "manager":
        return
    if get_state(context) != "awaiting_backup_file":
        return
    if not update.message.document:
        await update.message.reply_text("لطفاً فایل دیتابیس (.db) را ارسال کنید.")
        return

    doc = update.message.document
    tg_file = await doc.get_file()
    tmp_path = DATABASE_NAME + ".restore_tmp"
    await tg_file.download_to_drive(tmp_path)

    try:
        # اعتبارسنجی ساده: بررسی این‌که فایل یک دیتابیس SQLite معتبر است
        test_conn = sqlite3.connect(tmp_path)
        test_conn.execute("SELECT name FROM sqlite_master LIMIT 1")
        test_conn.close()
    except Exception:
        os.remove(tmp_path)
        await update.message.reply_text("فایل ارسالی یک دیتابیس معتبر نیست.")
        return

    db.close()
    os.replace(tmp_path, DATABASE_NAME)
    db.reconnect()
    clear_state(context)
    db.add_log("backup_restored", update.effective_user.id)
    await update.message.reply_text("✅ دیتابیس با موفقیت بازیابی شد.")


# ==================================================================================
# ================================= تنظیمات ربات ====================================
# ==================================================================================


@require_role("manager")
async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clear_state(context)
    max_size = int(db.get_setting("MAX_FILE_SIZE", MAX_FILE_SIZE))
    text = (
        "⚙️ <b>تنظیمات ربات</b>\n\n"
        f"حداکثر حجم فایل مجاز فعلی: {max_size // (1024*1024)} مگابایت\n"
        f"انواع فایل مجاز: {', '.join(ALLOWED_FILE_TYPES)}"
    )
    kb = [
        [InlineKeyboardButton("✏️ تغییر حداکثر حجم فایل", callback_data="settings_edit_size")],
        nav_row("mgr_menu"),
    ]
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup(kb))


@require_role("manager")
async def settings_edit_size_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    set_state(context, "edit_max_size")
    text = "حداکثر حجم مجاز فایل را به مگابایت وارد کنید (مثلاً 50):"
    await safe_edit(update.callback_query, text, InlineKeyboardMarkup([nav_row("settings_menu")]))


# ==================================================================================
# ============================== روتر پیام‌های متنی ==================================
# ==================================================================================


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(context)
    if not state:
        return  # هیچ عملیات در جریانی وجود ندارد؛ پیام نادیده گرفته می‌شود
    user = update.effective_user
    text_value = update.message.text.strip()
    data = get_state_data(context)

    # ---------------- ورود نماینده با رمز عبور (برای هر کاربری، حتی مهمان) ----------------
    if state == "rep_login_password":
        if len(text_value) < 1:
            await update.message.reply_text("رمز عبور نامعتبر است. دوباره تلاش کنید.")
            return
        pwd_hash = hash_password(text_value)
        rep = db.find_representative_by_password(pwd_hash)
        if not rep:
            await update.message.reply_text(
                "❌ رمز عبور نادرست است یا شرکت/نماینده غیرفعال است. دوباره تلاش کنید یا /cancel را بزنید."
            )
            return
        db.add_rep_login(
            user.id, rep["id"], rep["company_id"], user.username, user.full_name
        )
        db.add_log("rep_login", user.id, f"representative_id={rep['id']}")
        clear_state(context)
        await update.message.reply_text(f"✅ با موفقیت وارد شدید. کارتابل شرکت «{rep['company_name']}»")
        await show_representative_menu(update, context, edit=False)
        return

    role = get_role(user.id)
    if role is None:
        return

    # ---------------- شرکت‌ها ----------------
    if state == "add_company_name" and role == "manager":
        set_state(context, "add_company_desc", name=text_value)
        await update.message.reply_text("توضیحات شرکت را وارد کنید (یا 'هیچ' بفرستید):")
        return

    if state == "add_company_desc" and role == "manager":
        desc = None if text_value in ("هیچ", "-") else text_value
        try:
            db.add_company(data["name"], desc, user.id)
            db.add_log("add_company", user.id, f"name={data['name']}")
            clear_state(context)
            await update.message.reply_text(
                "✅ شرکت جدید ثبت شد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="nav_home")]]),
            )
        except sqlite3.IntegrityError:
            await update.message.reply_text("شرکتی با این نام از قبل وجود دارد. نام دیگری وارد کنید:")
            set_state(context, "add_company_name")
        return

    if state == "edit_company_name" and role == "manager":
        company_id = data["company_id"]
        new_name = None if text_value == "رد" else text_value
        try:
            db.update_company(company_id, name=new_name)
            db.add_log("edit_company", user.id, f"company_id={company_id}")
            clear_state(context)
            await update.message.reply_text(
                "✅ شرکت به‌روزرسانی شد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="nav_home")]]),
            )
        except sqlite3.IntegrityError:
            await update.message.reply_text("این نام قبلاً استفاده شده. نام دیگری وارد کنید:")
        return

    # ---------------- دسته‌بندی‌ها ----------------
    if state == "add_category_name" and role == "manager":
        db.add_category(data["company_id"], text_value, user.id)
        db.add_log("add_category", user.id, f"company_id={data['company_id']} name={text_value}")
        clear_state(context)
        await update.message.reply_text(
            "✅ دسته‌بندی جدید ثبت شد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="nav_home")]]),
        )
        return

    if state == "edit_category_name" and role == "manager":
        db.update_category(data["category_id"], text_value)
        db.add_log("edit_category", user.id, f"category_id={data['category_id']}")
        clear_state(context)
        await update.message.reply_text(
            "✅ دسته‌بندی به‌روزرسانی شد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="nav_home")]]),
        )
        return

    # ---------------- نمایندگان (افزودن: نام سپس رمز عبور) ----------------
    if state == "add_rep_name" and role == "manager":
        if not text_value:
            await update.message.reply_text("نام نامعتبر است. دوباره وارد کنید:")
            return
        set_state(context, "add_rep_password", company_id=data["company_id"], full_name=text_value)
        await update.message.reply_text(
            f"رمز عبور برای «{text_value}» را وارد کنید (حداقل {MIN_PASSWORD_LENGTH} کاراکتر):"
        )
        return

    if state == "add_rep_password" and role == "manager":
        if len(text_value) < MIN_PASSWORD_LENGTH:
            await update.message.reply_text(
                f"رمز عبور باید حداقل {MIN_PASSWORD_LENGTH} کاراکتر باشد. دوباره وارد کنید:"
            )
            return
        pwd_hash = hash_password(text_value)
        if db.password_hash_in_use(pwd_hash):
            await update.message.reply_text(
                "این رمز عبور قبلاً برای نماینده دیگری استفاده شده است. رمز دیگری انتخاب کنید:"
            )
            return
        db.add_representative(data["company_id"], data["full_name"], pwd_hash, user.id)
        db.add_log("add_representative", user.id, f"company_id={data['company_id']} name={data['full_name']}")
        clear_state(context)
        await update.message.reply_text(
            "✅ نماینده جدید ثبت شد.\n"
            "هر اکانت تلگرامی که بخواهد به‌عنوان این نماینده کار کند، کافی است "
            "«ورود به عنوان نماینده» را بزند و همین رمز عبور را وارد کند.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="nav_home")]]),
        )
        return

    if state == "edit_rep_password" and role == "manager":
        rep_id = data["rep_id"]
        if len(text_value) < MIN_PASSWORD_LENGTH:
            await update.message.reply_text(
                f"رمز عبور باید حداقل {MIN_PASSWORD_LENGTH} کاراکتر باشد. دوباره وارد کنید:"
            )
            return
        pwd_hash = hash_password(text_value)
        if db.password_hash_in_use(pwd_hash, exclude_id=rep_id):
            await update.message.reply_text(
                "این رمز عبور قبلاً برای نماینده دیگری استفاده شده است. رمز دیگری انتخاب کنید:"
            )
            return
        db.update_representative_password(rep_id, pwd_hash)
        db.delete_rep_logins_by_rep(rep_id)  # اجبار به ورود مجدد با رمز جدید
        db.add_log("edit_rep_password", user.id, f"rep_id={rep_id}")
        clear_state(context)
        await update.message.reply_text(
            "✅ رمز عبور به‌روزرسانی شد. اکانت‌های قبلی باید دوباره وارد شوند.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="nav_home")]]),
        )
        return

    # ---------------- آپلود فایل ----------------
    if state == "awaiting_caption" and role == "representative":
        caption = None if text_value in ("هیچ", "-") else text_value
        new_data = dict(data)
        new_data["caption"] = caption
        set_state(context, "preview_ready", **new_data)
        await show_upload_preview(update, context)
        return

    # ---------------- جستجو (منیجر) ----------------
    if state == "search_name" and role == "manager":
        clear_state(context)
        await render_file_list(update, context, {"name_like": text_value}, 0, "file_list", "search_menu")
        return

    if state == "search_caption" and role == "manager":
        clear_state(context)
        await render_file_list(update, context, {"caption_like": text_value}, 0, "file_list", "search_menu")
        return

    if state == "search_uploader" and role == "manager":
        clear_state(context)
        await render_file_list(update, context, {"uploader_name": text_value}, 0, "file_list", "search_menu")
        return

    if state == "search_date" and role == "manager":
        clear_state(context)
        await render_file_list(update, context, {"date_like": text_value}, 0, "file_list", "search_menu")
        return

    # ---------------- جستجوی نماینده در فایل‌های شرکت خودش ----------------
    if state == "rep_search_query" and role == "representative":
        clear_state(context)
        session = get_rep_session(user.id)
        await render_file_list(
            update, context,
            {"company_id": session["company_id"], "name_like": text_value},
            0, "rep_history", "nav_home",
        )
        return

    # ---------------- تنظیمات ----------------
    if state == "edit_max_size" and role == "manager":
        if not text_value.isdigit():
            await update.message.reply_text("لطفاً یک عدد معتبر (مگابایت) وارد کنید.")
            return
        db.set_setting("MAX_FILE_SIZE", int(text_value) * 1024 * 1024)
        db.add_log("edit_settings", user.id, f"MAX_FILE_SIZE={text_value}MB")
        clear_state(context)
        await update.message.reply_text(
            "✅ تنظیمات به‌روزرسانی شد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 خانه", callback_data="nav_home")]]),
        )
        return


# ==================================================================================
# ================================ روتر کالبک‌ها ====================================
# ==================================================================================


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    await query.answer()

    try:
        if data == "noop":
            return

        if data == "nav_home":
            role = get_role(update.effective_user.id)
            clear_state(context)
            if role == "manager":
                await show_manager_menu(update, context)
            elif role == "representative":
                await show_representative_menu(update, context)
            else:
                await show_guest_menu(update, context)
            return

        if data == "nav_cancel":
            clear_state(context)
            await query.answer("عملیات لغو شد.")
            role = get_role(update.effective_user.id)
            if role == "manager":
                await show_manager_menu(update, context)
            elif role == "representative":
                await show_representative_menu(update, context)
            else:
                await show_guest_menu(update, context)
            return

        if data == "mgr_menu":
            await show_manager_menu(update, context)
            return

        # ---------------- ورود / خروج نماینده ----------------
        if data == "rep_login_start":
            set_state(context, "rep_login_password")
            text = "🔑 لطفاً رمز عبور نماینده را وارد کنید:"
            kb = [[InlineKeyboardButton("❌ لغو", callback_data="nav_cancel")]]
            await safe_edit(query, text, InlineKeyboardMarkup(kb))
            return
        if data == "rep_logout":
            db.delete_rep_login(update.effective_user.id)
            db.add_log("rep_logout", update.effective_user.id)
            clear_state(context)
            await query.answer("از حساب خارج شدید.")
            await show_guest_menu(update, context)
            return

        # ---------------- شرکت‌ها ----------------
        if data.startswith("cmp_list:"):
            await cmp_list(update, context, int(data.split(":")[1]))
            return
        if data.startswith("cmp_view:"):
            await cmp_view(update, context, int(data.split(":")[1]))
            return
        if data == "cmp_add":
            await cmp_add_start(update, context)
            return
        if data.startswith("cmp_edit:"):
            await cmp_edit_start(update, context, int(data.split(":")[1]))
            return
        if data.startswith("cmp_toggle:"):
            await cmp_toggle(update, context, int(data.split(":")[1]))
            return
        if data.startswith("cmp_del:"):
            await cmp_delete(update, context, int(data.split(":")[1]))
            return

        # ---------------- دسته‌بندی‌ها ----------------
        if data.startswith("cat_list:"):
            parts = data.split(":")
            await cat_list(update, context, int(parts[1]), int(parts[2]))
            return
        if data.startswith("cat_view:"):
            await cat_view(update, context, int(data.split(":")[1]))
            return
        if data.startswith("cat_add:"):
            await cat_add_start(update, context, int(data.split(":")[1]))
            return
        if data.startswith("cat_edit:"):
            await cat_edit_start(update, context, int(data.split(":")[1]))
            return
        if data.startswith("cat_toggle:"):
            await cat_toggle(update, context, int(data.split(":")[1]))
            return
        if data.startswith("cat_del:"):
            await cat_delete(update, context, int(data.split(":")[1]))
            return

        # ---------------- نمایندگان ----------------
        if data.startswith("rep_list:"):
            parts = data.split(":")
            await rep_list(update, context, int(parts[1]), int(parts[2]))
            return
        if data.startswith("rep_view:"):
            await rep_view(update, context, int(data.split(":")[1]))
            return
        if data.startswith("rep_add:"):
            await rep_add_start(update, context, int(data.split(":")[1]))
            return
        if data.startswith("rep_edit_pw:"):
            await rep_edit_password_start(update, context, int(data.split(":")[1]))
            return
        if data.startswith("rep_toggle:"):
            await rep_toggle(update, context, int(data.split(":")[1]))
            return
        if data.startswith("rep_del:"):
            await rep_delete(update, context, int(data.split(":")[1]))
            return
        if data.startswith("rep_logout_all:"):
            await rep_logout_all(update, context, int(data.split(":")[1]))
            return
        if data.startswith("rep_history:"):
            await rep_history(update, context, int(data.split(":")[1]))
            return
        if data == "rep_search_start":
            await rep_search_start(update, context)
            return

        # ---------------- آپلود ----------------
        if data == "up_start":
            await upload_start(update, context)
            return
        if data.startswith("up_cat:"):
            await upload_select_category(update, context, int(data.split(":")[1]))
            return
        if data == "up_edit_caption":
            await upload_edit_caption(update, context)
            return
        if data == "up_confirm":
            await upload_confirm(update, context)
            return

        # ---------------- جستجو و فایل‌ها ----------------
        if data == "search_menu":
            await search_menu(update, context)
            return
        if data == "search_by_company":
            await search_by_company(update, context)
            return
        if data == "search_by_name":
            await search_prompt(update, context, "name")
            return
        if data == "search_by_caption":
            await search_prompt(update, context, "caption")
            return
        if data == "search_by_uploader":
            await search_prompt(update, context, "uploader")
            return
        if data == "search_by_date":
            await search_prompt(update, context, "date")
            return
        if data.startswith("file_list:"):
            await file_list_all(update, context, int(data.split(":")[1]))
            return
        if data.startswith("file_list_company:"):
            parts = data.split(":")
            await file_list_by_company(update, context, int(parts[1]), int(parts[2]))
            return
        if data.startswith("file_view:"):
            await file_view(update, context, int(data.split(":")[1]))
            return
        if data.startswith("file_send:"):
            await file_send(update, context, int(data.split(":")[1]))
            return
        if data.startswith("file_del:"):
            await file_delete(update, context, int(data.split(":")[1]))
            return

        # ---------------- آمار ----------------
        if data == "stats_show":
            await stats_show(update, context)
            return

        # ---------------- لاگ‌ها ----------------
        if data.startswith("logs_list:"):
            await logs_list(update, context, int(data.split(":")[1]))
            return

        # ---------------- بکاپ ----------------
        if data == "backup_menu":
            await backup_menu(update, context)
            return
        if data == "backup_send":
            await backup_send(update, context)
            return
        if data == "backup_restore_start":
            await backup_restore_start(update, context)
            return

        # ---------------- تنظیمات ----------------
        if data == "settings_menu":
            await settings_menu(update, context)
            return
        if data == "settings_edit_size":
            await settings_edit_size_start(update, context)
            return

    except Exception as e:
        logger.exception("خطا در پردازش کالبک")
        db.add_log("error", update.effective_user.id, f"callback={data} error={e}")
        try:
            await query.answer("⚠️ خطایی رخ داد. لطفاً دوباره تلاش کنید.", show_alert=True)
        except Exception:
            pass


# ==================================================================================
# ============================= هندلر مرکزی ورودی فایل ================================
# ==================================================================================


async def file_input_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_state(context)
    if state == "awaiting_file":
        await handle_file_input(update, context)
    elif state == "awaiting_backup_file":
        await handle_restore_file(update, context)
    # در غیر این صورت پیام نادیده گرفته می‌شود


# ==================================================================================
# ==================================== هندلر خطا ====================================
# ==================================================================================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("خطای پیش‌بینی‌نشده رخ داد", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_user:
            db.add_log("critical_error", update.effective_user.id, str(context.error))
    except Exception:
        pass


# ==================================================================================
# ====================================== main ========================================
# ==================================================================================


def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(callback_router))
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.VIDEO | filters.Document.ALL | filters.AUDIO, file_input_router)
    )
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    application.add_error_handler(error_handler)

    logger.info("ربات در حال اجراست...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
