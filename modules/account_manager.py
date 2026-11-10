"""
account_manager.py - Quản lý tập trung các tài khoản FPT / Inside / MyPT
Lưu trữ danh sách tài khoản định danh trong file accounts.json để tái sử dụng và cập nhật mật khẩu dễ dàng.
"""
import os
import json
import logging

log = logging.getLogger("account_manager")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ACCOUNTS_FILE = os.path.join(BASE_DIR, "accounts.json")


def _get_initial_accounts() -> list:
    """Tạo danh sách tài khoản ban đầu nếu accounts.json chưa có."""
    # Lấy thông tin từ config.py và user_config.json để không mất mật khẩu của user
    import config

    user_cfg_file = os.path.join(BASE_DIR, "user_config.json")
    user_cfg = {}
    if os.path.exists(user_cfg_file):
        try:
            with open(user_cfg_file, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
        except Exception:
            pass

    accs = [
        {
            "id": "phongnh5",
            "name": "Nguyễn Hồng Phong (phongnh5)",
            "email": getattr(config, "EMAIL", "phuongnam.phongnh5@fpt.net"),
            "email_password": getattr(config, "EMAIL_PASSWORD", ""),
            "inside_account": getattr(config, "INSIDE_ACCOUNT", "phuongnam.phongnh5"),
            "inside_password": getattr(config, "INSIDE_PASSWORD", ""),
            "use_inside_otp": True,
            "totp_secret": getattr(config, "TOTP_SECRET", ""),
            "is_default": user_cfg.get("inside_account") != "phuongnam.giangvt",
        }
    ]

    # Nếu có tài khoản trong user_config.json khác phongnh5 thì thêm vào
    if user_cfg.get("inside_account") and user_cfg.get("inside_account") != "phuongnam.phongnh5":
        inside_user = user_cfg.get("inside_account", "")
        uid = inside_user.split(".")[-1] if "." in inside_user else inside_user
        accs.append({
            "id": uid,
            "name": f"Tài khoản {inside_user}",
            "email": user_cfg.get("email", ""),
            "email_password": user_cfg.get("email_password", ""),
            "inside_account": inside_user,
            "inside_password": user_cfg.get("inside_password", ""),
            "use_inside_otp": bool(user_cfg.get("use_inside_otp", False)),
            "totp_secret": user_cfg.get("totp_secret", ""),
            "is_default": True,
        })
        accs[0]["is_default"] = False

    return accs


def load_accounts() -> list:
    """Đọc danh sách tài khoản từ accounts.json. Tạo mới nếu chưa có."""
    if not os.path.exists(ACCOUNTS_FILE):
        accs = _get_initial_accounts()
        save_accounts(accs)
        return accs

    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            accs = json.load(f)
            if not isinstance(accs, list) or len(accs) == 0:
                accs = _get_initial_accounts()
                save_accounts(accs)
            return accs
    except Exception as e:
        log.error(f"Lỗi đọc {ACCOUNTS_FILE}: {e}")
        return _get_initial_accounts()


def save_accounts(accounts: list) -> bool:
    """Lưu danh sách tài khoản vào accounts.json."""
    try:
        with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
            json.dump(accounts, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log.error(f"Lỗi lưu {ACCOUNTS_FILE}: {e}")
        return False


def get_active_account() -> dict:
    """Lấy tài khoản đang được chọn làm mặc định (hoặc tài khoản đầu tiên)."""
    accs = load_accounts()
    for a in accs:
        if a.get("is_default"):
            return a
    if accs:
        return accs[0]
    return {
        "id": "default",
        "name": "Mặc định",
        "email": "",
        "email_password": "",
        "inside_account": "",
        "inside_password": "",
        "use_inside_otp": True,
        "totp_secret": "",
        "is_default": True,
    }


def get_account_by_id(account_id: str) -> dict:
    """Tìm tài khoản theo ID / định danh."""
    if not account_id:
        return get_active_account()
    accs = load_accounts()
    for a in accs:
        if a.get("id") == account_id:
            return a
    return get_active_account()


def set_active_account(account_id: str) -> tuple[bool, str]:
    """Chọn một tài khoản làm mặc định/đang kích hoạt."""
    accs = load_accounts()
    found = False
    for a in accs:
        if a.get("id") == account_id:
            a["is_default"] = True
            found = True
        else:
            a["is_default"] = False
    if not found:
        return False, f"Không tìm thấy tài khoản với mã '{account_id}'"
    save_accounts(accs)
    return True, f"Đã chuyển sang tài khoản '{account_id}'"


def upsert_account(account_data: dict) -> tuple[bool, str]:
    """Thêm mới hoặc cập nhật thông tin/mật khẩu tài khoản theo ID định danh."""
    acc_id = str(account_data.get("id") or "").strip().lower()
    if not acc_id:
        # Nếu chưa có ID thì tự sinh từ inside_account hoặc email
        inside = str(account_data.get("inside_account") or "").strip()
        email = str(account_data.get("email") or "").strip()
        acc_id = (inside.split(".")[-1] if "." in inside else inside) or (email.split("@")[0]) or "acc"
        acc_id = acc_id.lower()

    account_data["id"] = acc_id
    accs = load_accounts()
    
    # Kiểm tra xem tài khoản đã tồn tại chưa
    idx = -1
    for i, a in enumerate(accs):
        if a.get("id") == acc_id:
            idx = i
            break

    is_def = account_data.get("is_default", False)
    if is_def:
        for a in accs:
            a["is_default"] = False

    cleaned_acc = {
        "id": acc_id,
        "name": account_data.get("name") or acc_id,
        "email": str(account_data.get("email") or "").strip(),
        "email_password": str(account_data.get("email_password") or "").strip(),
        "inside_account": str(account_data.get("inside_account") or "").strip(),
        "inside_password": str(account_data.get("inside_password") or "").strip(),
        "use_inside_otp": bool(account_data.get("use_inside_otp", True)),
        "totp_secret": str(account_data.get("totp_secret") or "").strip(),
        "is_default": bool(is_def or (idx == -1 and len(accs) == 0)),
    }

    if idx >= 0:
        # Cập nhật (nếu trường mật khẩu để trống thì giữ nguyên mật khẩu cũ)
        if not cleaned_acc["email_password"]:
            cleaned_acc["email_password"] = accs[idx].get("email_password", "")
        if not cleaned_acc["inside_password"]:
            cleaned_acc["inside_password"] = accs[idx].get("inside_password", "")
        if not cleaned_acc["totp_secret"] and accs[idx].get("totp_secret"):
            cleaned_acc["totp_secret"] = accs[idx].get("totp_secret", "")
        accs[idx] = cleaned_acc
        msg = f"Đã cập nhật thông tin tài khoản '{acc_id}'"
    else:
        accs.append(cleaned_acc)
        msg = f"Đã thêm mới tài khoản '{acc_id}'"

    # Đảm bảo luôn có ít nhất 1 tài khoản là is_default
    if not any(a.get("is_default") for a in accs) and accs:
        accs[0]["is_default"] = True

    save_accounts(accs)
    return True, msg


def delete_account(account_id: str) -> tuple[bool, str]:
    """Xóa tài khoản theo ID."""
    accs = load_accounts()
    if len(accs) <= 1:
        return False, "Không thể xóa tài khoản duy nhất còn lại."

    new_accs = [a for a in accs if a.get("id") != account_id]
    if len(new_accs) == len(accs):
        return False, f"Không tìm thấy tài khoản '{account_id}'"

    # Nếu tài khoản bị xóa đang là default, đặt tài khoản đầu tiên làm default
    if not any(a.get("is_default") for a in new_accs):
        new_accs[0]["is_default"] = True

    save_accounts(new_accs)
    return True, f"Đã xóa tài khoản '{account_id}'"
