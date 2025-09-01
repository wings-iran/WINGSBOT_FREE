import requests
import uuid
from datetime import datetime, timedelta
import json
import re
from urllib.parse import urlsplit
from .config import logger
from .db import query_db
import time as _time


class BasePanelAPI:
    async def get_all_users(self):
        raise NotImplementedError

    async def get_user(self, username):
        raise NotImplementedError

    async def renew_user_in_panel(self, username, plan):
        raise NotImplementedError

    async def create_user(self, user_id, plan):
        raise NotImplementedError


class MarzbanAPI(BasePanelAPI):
    def __init__(self, panel_row):
        self.panel_id = panel_row['id']
        self.base_url = panel_row['url'].rstrip('/')
        self.username = panel_row['username']
        self.password = panel_row['password']
        self.session = requests.Session()
        self.access_token = None

    def get_token(self):
        if not all([self.base_url, self.username, self.password]):
            logger.error("Marzban panel credentials are not set for this panel.")
            return False
        try:
            r = self.session.post(
                f"{self.base_url}/api/admin/token",
                data={'username': self.username, 'password': self.password},
                headers={'Content-Type': 'application/x-www-form-urlencoded', 'accept': 'application/json'},
                timeout=10,
            )
            r.raise_for_status()
            self.access_token = r.json().get('access_token')
            return True
        except requests.RequestException as e:
            logger.error(f"Failed to get Marzban token for {self.base_url}: {e}")
            return False

    async def get_all_users(self):
        if not self.access_token and not self.get_token():
            return None, "خطا در اتصال به پنل"
        headers = {'Authorization': f'Bearer {self.access_token}', 'accept': 'application/json'}
        try:
            r = self.session.get(f"{self.base_url}/api/users", headers=headers, timeout=20)
            r.raise_for_status()
            return r.json().get('users', []), "Success"
        except requests.RequestException as e:
            logger.error(f"Failed to get all users from {self.base_url}: {e}")
            return None, f"خطای پنل: {e}"

    def list_inbounds(self):
        # Try to fetch inbounds from Marzban API; tries multiple endpoints for compatibility
        if not self.access_token and not self.get_token():
            return None, "خطا در اتصال به پنل"
        headers = {'Authorization': f'Bearer {self.access_token}', 'accept': 'application/json'}
        endpoints = [
            f"{self.base_url}/api/inbounds",
            f"{self.base_url}/api/inbound",
            f"{self.base_url}/inbounds",
            f"{self.base_url}/api/config",
        ]
        last_error = None
        for url in endpoints:
            try:
                r = self.session.get(url, headers=headers, timeout=12)
                if r.status_code != 200:
                    last_error = f"HTTP {r.status_code} @ {url}"
                    continue
                try:
                    data = r.json()
                except ValueError:
                    last_error = f"non-JSON response @ {url}"
                    continue
                # Common shapes: {'inbounds': [...] } or list
                items = None
                if isinstance(data, dict):
                    if isinstance(data.get('inbounds'), list):
                        items = data.get('inbounds')
                    elif isinstance(data.get('obj'), list):
                        items = data.get('obj')
                if items is None and isinstance(data, list):
                    items = data
                if not isinstance(items, list):
                    # Maybe config returns nested inbounds
                    if isinstance(data, dict) and isinstance(data.get('config', {}).get('inbounds'), list):
                        items = data.get('config', {}).get('inbounds')
                if not isinstance(items, list):
                    last_error = "ساختار اینباندها قابل تشخیص نیست"
                    continue
                inbounds = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    inbounds.append({
                        'id': it.get('id') or it.get('tag') or it.get('remark') or '',
                        'remark': it.get('remark') or it.get('tag') or str(it.get('id') or ''),
                        'protocol': it.get('protocol') or it.get('type') or 'unknown',
                        'port': it.get('port') or 0,
                        'tag': it.get('tag') or it.get('remark') or str(it.get('id') or ''),
                    })
                return inbounds, "Success"
            except requests.RequestException as e:
                last_error = str(e)
                continue
        return None, (last_error or "Unknown")

    async def get_user(self, marzban_username):
        if not self.access_token and not self.get_token():
            return None, "خطا در اتصال به پنل"
        headers = {'Authorization': f'Bearer {self.access_token}', 'accept': 'application/json'}
        try:
            r = self.session.get(f"{self.base_url}/api/user/{marzban_username}", headers=headers, timeout=10)
            if r.status_code == 404:
                return None, "کاربر یافت نشد"
            r.raise_for_status()
            return r.json(), "Success"
        except requests.RequestException as e:
            logger.error(f"Failed to get user {marzban_username}: {e}")
            return None, f"خطای پنل: {e}"

    def revoke_subscription(self, marzban_username: str):
        # Try to revoke/rotate subscription URL for a user using common Marzban endpoints
        if not self.access_token and not self.get_token():
            return False, "توکن دریافت نشد"
        headers = {'Authorization': f'Bearer {self.access_token}', 'accept': 'application/json'}
        candidates = [
            f"{self.base_url}/api/user/{marzban_username}/revoke-sub",
            f"{self.base_url}/api/user/{marzban_username}/revoke_sub",
            f"{self.base_url}/api/user/{marzban_username}/subscription/revoke",
            f"{self.base_url}/api/user/{marzban_username}/revoke",
        ]
        last = None
        for url in candidates:
            try:
                r = self.session.post(url, headers=headers, timeout=12)
                if r.status_code in (200, 201, 202, 204):
                    return True, "Success"
                last = f"HTTP {r.status_code} @ {url}"
            except requests.RequestException as e:
                last = str(e)
                continue
        return False, (last or "Unknown")

    async def renew_user_in_panel(self, marzban_username, plan):
        current_user_info, message = await self.get_user(marzban_username)
        if not current_user_info:
            return None, f"کاربر {marzban_username} برای تمدید یافت نشد."
        current_expire = current_user_info.get('expire') or int(datetime.now().timestamp())
        base_timestamp = max(current_expire, int(datetime.now().timestamp()))
        additional_days_in_seconds = int(plan['duration_days']) * 86400
        new_expire_timestamp = base_timestamp + additional_days_in_seconds
        current_data_limit = current_user_info.get('data_limit', 0)
        additional_data_bytes = int(float(plan['traffic_gb']) * 1024 * 1024 * 1024)
        new_data_limit_bytes = current_data_limit + additional_data_bytes
        update_data = {"expire": new_expire_timestamp, "data_limit": new_data_limit_bytes}
        headers = {'Authorization': f'Bearer {self.access_token}', 'accept': 'application/json', 'Content-Type': 'application/json'}
        try:
            r = self.session.put(f"{self.base_url}/api/user/{marzban_username}", json=update_data, headers=headers, timeout=15)
            r.raise_for_status()
            return r.json(), "Success"
        except requests.RequestException as e:
            error_detail = "Unknown error"
            if e.response:
                try:
                    error_detail = e.response.json().get('detail', e.response.text)
                except Exception:
                    error_detail = e.response.text
            logger.error(f"Failed to renew user {marzban_username}: {e} - {error_detail}")
            return None, f"خطای پنل هنگام تمدید: {error_detail}"

    async def create_user(self, user_id, plan):
        if not self.access_token and not self.get_token():
            return None, None, "خطا در اتصال به پنل. لطفا تنظیمات را بررسی کنید."

        manual_inbounds = query_db("SELECT protocol, tag FROM panel_inbounds WHERE panel_id = ?", (self.panel_id,)) or []
        # Fallback: auto-discover inbounds from Marzban if none configured locally
        if not manual_inbounds:
            discovered, _msg = self.list_inbounds()
            if discovered:
                for ib in discovered:
                    proto = (ib.get('protocol') or '').lower()
                    tag = ib.get('tag') or ib.get('remark') or str(ib.get('id') or '')
                    if proto and tag:
                        manual_inbounds.append({'protocol': proto, 'tag': tag})
            if not manual_inbounds:
                return None, None, "خطا: اینباندی یافت نشد. ابتدا یک اینباند در پنل بسازید."

        inbounds_by_protocol = {}
        for inbound in manual_inbounds:
            protocol = inbound.get('protocol')
            tag = inbound.get('tag')
            if protocol and tag:
                if protocol not in inbounds_by_protocol:
                    inbounds_by_protocol[protocol] = []
                inbounds_by_protocol[protocol].append(tag)

        if not inbounds_by_protocol:
            return None, None, "خطا: اینباندهای تنظیم شده در دیتابیس معتبر نیستند."

        new_username = f"user_{user_id}_{uuid.uuid4().hex[:6]}"
        traffic_gb = float(plan['traffic_gb'])
        data_limit_bytes = int(traffic_gb * 1024 * 1024 * 1024) if traffic_gb > 0 else 0
        expire_timestamp = int((datetime.now() + timedelta(days=int(plan['duration_days']))).timestamp()) if int(plan['duration_days']) > 0 else 0

        proxies_to_add = {}
        for protocol in inbounds_by_protocol.keys():
            proxies_to_add[protocol] = {"flow": "xtls-rprx-vision"} if protocol == "vless" else {}

        user_data = {
            "status": "active",
            "username": new_username,
            "note": "",
            "proxies": proxies_to_add,
            "data_limit": data_limit_bytes,
            "expire": expire_timestamp,
            "data_limit_reset_strategy": "no_reset",
            "inbounds": inbounds_by_protocol,
        }

        headers = {'Authorization': f'Bearer {self.access_token}', 'accept': 'application/json', 'Content-Type': 'application/json'}
        try:
            r = self.session.post(f"{self.base_url}/api/user", json=user_data, headers=headers, timeout=15)
            r.raise_for_status()
            user_info = r.json()
            subscription_path = user_info.get('subscription_url')
            if not subscription_path:
                links = "\n".join(user_info.get('links', []))
                return new_username, links, "Success"

            full_subscription_link = (
                f"{self.base_url}{subscription_path}" if not subscription_path.startswith('http') else subscription_path
            )
            logger.info(f"Successfully created Marzban user: {new_username} with inbounds: {inbounds_by_protocol}")
            return new_username, full_subscription_link, "Success"
        except requests.RequestException as e:
            error_detail = "Unknown error"
            if e.response:
                try:
                    error_detail_json = e.response.json().get('detail')
                    if isinstance(error_detail_json, list):
                        error_detail = " ".join([d.get('msg', '') for d in error_detail_json if 'msg' in d])
                    elif isinstance(error_detail_json, str):
                        error_detail = error_detail_json
                    else:
                        error_detail = e.response.text
                except Exception:
                    error_detail = e.response.text
            logger.error(f"Failed to create new user: {e} - {error_detail}")
            return None, None, f"خطای پنل: {error_detail}"


class XuiAPI(BasePanelAPI):
    """Alireza (X-UI) support using uppercase /xui/API endpoints as per provided method."""

    def __init__(self, panel_row):
        self.panel_id = panel_row['id']
        self.base_url = panel_row['url'].rstrip('/')
        self.username = panel_row['username']
        self.password = panel_row['password']
        self.sub_base = (panel_row.get('sub_base') or '').strip().rstrip('/') if isinstance(panel_row, dict) else ''
        self.session = requests.Session()
        self._json_headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
        }

    def get_token(self):
        # Try form login first (more compatible across versions)
        try:
            try:
                self.session.get(f"{self.base_url}/login", timeout=8)
            except requests.RequestException:
                pass
            form_headers = {
                'Accept': 'text/html,application/json',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
            }
            resp = self.session.post(
                f"{self.base_url}/login",
                data={"username": self.username, "password": self.password},
                headers=form_headers,
                allow_redirects=False,
                timeout=12,
            )
            if resp.status_code in (200, 204, 302, 303):
                return True
        except requests.RequestException:
            pass
        # Fallback to JSON login
        try:
            resp = self.session.post(
                f"{self.base_url}/login",
                json={"username": self.username, "password": self.password},
                headers=self._json_headers,
                timeout=12,
            )
            if resp.status_code in (200, 204, 302, 303):
                return True
        except requests.RequestException as e:
            logger.error(f"X-UI login error: {e}")
        return False

    def list_inbounds(self):
        if not self.get_token():
            return None, "خطا در ورود به پنل X-UI"
        try:
            resp = self.session.get(
                f"{self.base_url}/xui/API/inbounds/",
                headers={'Accept': 'application/json'},
                timeout=10,
            )
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}"
            data = resp.json()
            items = data.get('obj') if isinstance(data, dict) else data
            if not isinstance(items, list):
                return None, "لیست اینباند نامعتبر است"
            inbounds = []
            for it in items:
                inbounds.append({
                    'id': it.get('id'),
                    'remark': it.get('remark') or it.get('tag') or str(it.get('id')),
                    'protocol': it.get('protocol') or it.get('type') or 'unknown',
                    'port': it.get('port') or it.get('listen_port') or 0,
                })
            return inbounds, "Success"
        except requests.RequestException as e:
            logger.error(f"X-UI list_inbounds error: {e}")
            return None, str(e)
        except ValueError as ve:
            logger.error(f"X-UI JSON parse error for /xui/API/inbounds/: {ve}")
            return None, "JSON parse error"

    def create_user_on_inbound(self, inbound_id: int, user_id: int, plan):
        if not self.get_token():
            return None, None, "خطا در ورود به پنل X-UI"
        try:
            new_username = f"user_{user_id}_{uuid.uuid4().hex[:6]}"
            import random, string
            subid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
            try:
                traffic_gb = float(plan['traffic_gb'])
            except Exception:
                traffic_gb = 0.0
            total_bytes = int(traffic_gb * (1024 ** 3)) if traffic_gb > 0 else 0
            try:
                days = int(plan['duration_days'])
                expiry_ms = int((datetime.now() + timedelta(days=days)).timestamp() * 1000) if days > 0 else 0
            except Exception:
                expiry_ms = 0

            settings = json.dumps({
                "clients": [{
                    "id": str(uuid.uuid4()),
                    "email": new_username,
                    "totalGB": total_bytes,
                    "expiryTime": expiry_ms,
                    "enable": True,
                    "limitIp": 0,
                    "subId": subid,
                    "reset": 0
                }]
            })
            payload = {"id": int(inbound_id), "settings": settings}
            resp = self.session.post(
                f"{self.base_url}/xui/API/inbounds/addClient",
                headers={'Content-Type': 'application/json'},
                json=payload,
                timeout=15,
            )
            if resp.status_code not in (200, 201):
                return None, None, f"HTTP {resp.status_code}: {resp.text[:120]}"
            if self.sub_base:
                origin = self.sub_base
            else:
                parts = urlsplit(self.base_url)
                host = parts.hostname or ''
                port = ''
                if parts.port and not ((parts.scheme == 'http' and parts.port == 80) or (parts.scheme == 'https' and parts.port == 443)):
                    port = f":{parts.port}"
                origin = f"{parts.scheme}://{host}{port}"
            sub_link = f"{origin}/sub/{subid}?name={subid}"
            return new_username, sub_link, "Success"
        except requests.RequestException as e:
            logger.error(f"X-UI create_user_on_inbound error: {e}")
            return None, None, str(e)

    async def get_all_users(self):
        return None, "Not supported for X-UI"

    async def get_user(self, username):
        # Find client by email across inbounds and map to common fields
        if not self.get_token():
            return None, "خطا در ورود به پنل X-UI"
        inbounds, msg = self.list_inbounds()
        if not inbounds:
            return None, msg
        for ib in inbounds:
            inbound_id = ib.get('id')
            inbound = self._fetch_inbound_detail(inbound_id)
            if not inbound:
                continue
            settings_str = inbound.get('settings')
            try:
                settings_obj = json.loads(settings_str) if isinstance(settings_str, str) else {}
            except Exception:
                settings_obj = {}
            clients = settings_obj.get('clients') or []
            if not isinstance(clients, list):
                continue
            for c in clients:
                if c.get('email') == username:
                    total_bytes = int(c.get('totalGB', 0) or 0)
                    expiry_ms = int(c.get('expiryTime', 0) or 0)
                    expire = int(expiry_ms / 1000) if expiry_ms > 0 else 0
                    subid = c.get('subId') or ''
                    # Build subscription URL
                    if self.sub_base:
                        origin = self.sub_base
                    else:
                        parts = urlsplit(self.base_url)
                        host = parts.hostname or ''
                        port = ''
                        if parts.port and not ((parts.scheme == 'http' and parts.port == 80) or (parts.scheme == 'https' and parts.port == 443)):
                            port = f":{parts.port}"
                        origin = f"{parts.scheme}://{host}{port}"
                    sub_link = f"{origin}/sub/{subid}?name={subid}" if subid else ''
                    return {
                        'data_limit': total_bytes,
                        'used_traffic': 0,
                        'expire': expire,
                        'subscription_url': sub_link,
                    }, "Success"
        return None, "کاربر یافت نشد"

    def _fetch_inbound_detail(self, inbound_id: int):
        # Try multiple endpoints to fetch inbound detail including settings
        paths = [
            f"/xui/API/inbounds/get/{inbound_id}",
            f"/panel/API/inbounds/get/{inbound_id}",
            f"/xui/api/inbounds/get/{inbound_id}",
            f"/panel/api/inbounds/get/{inbound_id}",
        ]
        for p in paths:
            try:
                resp = self.session.get(f"{self.base_url}{p}", headers={'Accept': 'application/json'}, timeout=12)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                inbound = data.get('obj') if isinstance(data, dict) else data
                if isinstance(inbound, dict):
                    return inbound
            except Exception:
                continue
        return None

    async def renew_user_in_panel(self, username, plan):
        # Login first
        if not self.get_token():
            return None, "خطا در ورود به پنل X-UI"
        inbounds, msg = self.list_inbounds()
        if not inbounds:
            return None, msg
        now_ms = int(datetime.now().timestamp() * 1000)
        add_bytes = 0
        try:
            add_bytes = int(float(plan['traffic_gb']) * (1024 ** 3))
        except Exception:
            add_bytes = 0
        add_ms = 0
        try:
            days = int(plan['duration_days'])
            add_ms = days * 86400 * 1000 if days > 0 else 0
        except Exception:
            add_ms = 0
        for ib in inbounds:
            inbound_id = ib.get('id')
            inbound = self._fetch_inbound_detail(inbound_id)
            if not inbound:
                continue
            settings_str = inbound.get('settings')
            clients = []
            try:
                if isinstance(settings_str, str):
                    settings_obj = json.loads(settings_str)
                    clients = settings_obj.get('clients', [])
            except Exception:
                clients = []
            if not isinstance(clients, list):
                continue
            for idx, c in enumerate(clients):
                if c.get('email') == username:
                    current_exp = int(c.get('expiryTime', 0) or 0)
                    base = max(current_exp, now_ms)
                    target_exp = base + (add_ms if add_ms > 0 else 0)
                    new_total = int(c.get('totalGB', 0) or 0) + (add_bytes if add_bytes > 0 else 0)
                    updated = dict(c)
                    updated['expiryTime'] = target_exp
                    updated['totalGB'] = new_total
                    # Endpoint variants (prioritize updateClient/{uuid})
                    uuid_old = c.get('id') or c.get('uuid') or ''
                    base_eps = [
                        "/xui/API/inbounds/updateClient",
                        "/panel/API/inbounds/updateClient",
                        "/xui/api/inbounds/updateClient",
                        "/panel/api/inbounds/updateClient",
                    ]
                    endpoints = ([f"{e}/{uuid_old}" for e in base_eps] + base_eps) if uuid_old else base_eps
                    json_headers = {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}
                    form_headers = {'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest'}
                    last_err = None
                    for up in endpoints:
                        try:
                            # A) settings with single updated client
                            payload_settings_single = {"id": int(inbound_id), "settings": json.dumps({"clients": [updated]})}
                            resp = self.session.post(f"{self.base_url}{up}", headers=json_headers, json=payload_settings_single, timeout=15)
                            if resp.status_code in (200, 201):
                                ref = self._fetch_inbound_detail(inbound_id)
                                try:
                                    robj = json.loads(ref.get('settings')) if isinstance(ref.get('settings'), str) else (ref.get('settings') or {})
                                except Exception:
                                    robj = {}
                                for c2 in (robj.get('clients') or []):
                                    if c2.get('email') == username and int(c2.get('expiryTime', 0) or 0) == target_exp and int(c2.get('totalGB', 0) or 0) == new_total:
                                        return updated, "Success"
                            # B) clients array JSON
                            payload_clients = {"id": int(inbound_id), "clients": [updated]}
                            resp = self.session.post(f"{self.base_url}{up}", headers=json_headers, json=payload_clients, timeout=15)
                            if resp.status_code in (200, 201):
                                ref = self._fetch_inbound_detail(inbound_id)
                                try:
                                    robj = json.loads(ref.get('settings')) if isinstance(ref.get('settings'), str) else (ref.get('settings') or {})
                                except Exception:
                                    robj = {}
                                for c2 in (robj.get('clients') or []):
                                    if c2.get('email') == username and int(c2.get('expiryTime', 0) or 0) == target_exp and int(c2.get('totalGB', 0) or 0) == new_total:
                                        return updated, "Success"
                            # C) form-urlencoded with settings (single)
                            resp = self.session.post(f"{self.base_url}{up}", headers=form_headers, data={"id": str(int(inbound_id)), "settings": json.dumps({"clients": [updated]})}, timeout=15)
                            if resp.status_code in (200, 201):
                                ref = self._fetch_inbound_detail(inbound_id)
                                try:
                                    robj = json.loads(ref.get('settings')) if isinstance(ref.get('settings'), str) else (ref.get('settings') or {})
                                except Exception:
                                    robj = {}
                                for c2 in (robj.get('clients') or []):
                                    if c2.get('email') == username and int(c2.get('expiryTime', 0) or 0) == target_exp and int(c2.get('totalGB', 0) or 0) == new_total:
                                        return updated, "Success"
                            # D) settings with full clients
                            full_clients = list(clients)
                            full_clients[idx] = updated
                            payload_settings_full = {"id": int(inbound_id), "settings": json.dumps({"clients": full_clients})}
                            resp = self.session.post(f"{self.base_url}{up}", headers=json_headers, json=payload_settings_full, timeout=15)
                            if resp.status_code in (200, 201):
                                ref = self._fetch_inbound_detail(inbound_id)
                                try:
                                    robj = json.loads(ref.get('settings')) if isinstance(ref.get('settings'), str) else (ref.get('settings') or {})
                                except Exception:
                                    robj = {}
                                for c2 in (robj.get('clients') or []):
                                    if c2.get('email') == username and int(c2.get('expiryTime', 0) or 0) == target_exp and int(c2.get('totalGB', 0) or 0) == new_total:
                                        return updated, "Success"
                            last_err = f"HTTP {resp.status_code}: {(resp.text or '')[:160]}"
                        except requests.RequestException as e:
                            last_err = str(e)
                            continue
                    return None, (last_err or "به‌روزرسانی کلاینت ناموفق بود")
        return None, "کلاینت برای تمدید یافت نشد"

    async def create_user(self, user_id, plan):
        return None, None, "برای X-UI ابتدا اینباند را انتخاب کنید."

    def renew_user_on_inbound(self, inbound_id: int, username: str, add_gb: float, add_days: int):
        # Login first
        if not self.get_token():
            return None, "خطا در ورود به پنل X-UI"
        inbound = self._fetch_inbound_detail(inbound_id)
        if not inbound:
            return None, "اینباند یافت نشد"
        try:
            now_ms = int(datetime.now().timestamp() * 1000)
            settings_str = inbound.get('settings')
            try:
                settings_obj = json.loads(settings_str) if isinstance(settings_str, str) else (settings_str or {})
            except Exception:
                settings_obj = {}
            clients = settings_obj.get('clients') or []
            if not isinstance(clients, list):
                return None, "ساختار کلاینت‌ها نامعتبر است"
            updated = None
            cur_total = 0
            cur_exp_raw = 0
            for c in clients:
                if c.get('email') == username:
                    current_exp = int(c.get('expiryTime', 0) or 0)
                    cur_exp_raw = current_exp
                    add_bytes = int(float(add_gb) * (1024 ** 3)) if add_gb and add_gb > 0 else 0
                    # detect seconds vs milliseconds based on current value magnitude
                    is_ms = current_exp > 10**11
                    now_unit = now_ms if is_ms else int(now_ms / 1000)
                    add_unit = (int(add_days) * 86400 * (1000 if is_ms else 1)) if add_days and int(add_days) > 0 else 0
                    base = max(current_exp, now_unit)
                    target_exp = base + add_unit if add_unit > 0 else current_exp
                    cur_total = int(c.get('totalGB', 0) or 0)
                    new_total = cur_total + (add_bytes if add_bytes > 0 else 0)
                    updated = dict(c)
                    updated['expiryTime'] = target_exp
                    updated['totalGB'] = new_total
                    uuid_old = c.get('id') or c.get('uuid') or ''
                    break
            if not updated:
                return None, "کلاینت یافت نشد"
            # Build endpoints per Postman: prioritize updateClient/{uuid}
            base_eps = [
                "/panel/api/inbounds/updateClient",
                "/xui/api/inbounds/updateClient",
                "/panel/API/inbounds/updateClient",
                "/xui/API/inbounds/updateClient",
                "/xui/api/inbound/updateClient",
            ]
            endpoints = ([f"{e}/{uuid_old}" for e in base_eps] + base_eps) if uuid_old else base_eps
            # Ensure fields expected by X-UI exist
            if 'alterId' not in updated:
                try:
                    updated['alterId'] = int(updated.get('alterId', 0) or 0)
                except Exception:
                    updated['alterId'] = 0
            if 'enable' not in updated:
                updated['enable'] = True
            # Build headers and payloads (match Postman curl: form-urlencoded first)
            json_headers = {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}
            form_headers = {'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest'}
            settings_payload = json.dumps({"clients": [updated]})
            payload_json = {"id": int(inbound_id), "settings": settings_payload}
            payload_form = {"id": str(int(inbound_id)), "settings": settings_payload}
            last_err = None; last_ep = None; last_code = None
            for ep in endpoints:
                try:
                    # A) form-urlencoded (as in provided curl)
                    r = self.session.post(f"{self.base_url}{ep}", headers=form_headers, data=payload_form, timeout=15)
                    if r.status_code in (200, 201):
                        # verify by refetching inbound
                        ref = self._fetch_inbound_detail(inbound_id)
                        try:
                            robj = json.loads(ref.get('settings')) if isinstance(ref.get('settings'), str) else (ref.get('settings') or {})
                        except Exception:
                            robj = {}
                        for c2 in (robj.get('clients') or []):
                            if c2.get('email') == username:
                                new_exp = int(c2.get('expiryTime', 0) or 0)
                                new_total_chk = int(c2.get('totalGB', 0) or 0)
                                # require growth when add requested
                                grew_total = (updated['totalGB'] > cur_total) if (updated['totalGB'] != cur_total) else (add_gb == 0)
                                grew_exp = (updated['expiryTime'] > cur_exp_raw) if (updated['expiryTime'] != cur_exp_raw) else (add_days == 0)
                                try:
                                    logger.info(f"X-UI renew verify A: before_total={cur_total} after_total={new_total_chk} before_exp={cur_exp_raw} after_exp={new_exp}")
                                except Exception:
                                    pass
                                if new_total_chk == updated['totalGB'] and (new_exp == updated['expiryTime'] or abs(new_exp - updated['expiryTime']) <= 5) and (grew_total or grew_exp):
                                    return updated, "Success"
                    # B) JSON body with settings string
                    r = self.session.post(f"{self.base_url}{ep}", headers=json_headers, json=payload_json, timeout=15)
                    if r.status_code in (200, 201):
                        ref = self._fetch_inbound_detail(inbound_id)
                        try:
                            robj = json.loads(ref.get('settings')) if isinstance(ref.get('settings'), str) else (ref.get('settings') or {})
                        except Exception:
                            robj = {}
                        for c2 in (robj.get('clients') or []):
                            if c2.get('email') == username:
                                new_exp = int(c2.get('expiryTime', 0) or 0)
                                new_total_chk = int(c2.get('totalGB', 0) or 0)
                                try:
                                    logger.info(f"X-UI renew verify B: before_total={cur_total} after_total={new_total_chk} before_exp={cur_exp_raw} after_exp={new_exp}")
                                except Exception:
                                    pass
                                grew_total = (updated['totalGB'] > cur_total) if (updated['totalGB'] != cur_total) else (add_gb == 0)
                                grew_exp = (updated['expiryTime'] > cur_exp_raw) if (updated['expiryTime'] != cur_exp_raw) else (add_days == 0)
                                if new_total_chk == updated['totalGB'] and (new_exp == updated['expiryTime'] or abs(new_exp - updated['expiryTime']) <= 5) and (grew_total or grew_exp):
                                    return updated, "Success"
                    # C) JSON body with clients array
                    r = self.session.post(f"{self.base_url}{ep}", headers=json_headers, json={"id": int(inbound_id), "clients": [updated]}, timeout=15)
                    if r.status_code in (200, 201):
                        ref = self._fetch_inbound_detail(inbound_id)
                        try:
                            robj = json.loads(ref.get('settings')) if isinstance(ref.get('settings'), str) else (ref.get('settings') or {})
                        except Exception:
                            robj = {}
                        for c2 in (robj.get('clients') or []):
                            if c2.get('email') == username:
                                new_exp = int(c2.get('expiryTime', 0) or 0)
                                new_total_chk = int(c2.get('totalGB', 0) or 0)
                                try:
                                    logger.info(f"X-UI renew verify C: before_total={cur_total} after_total={new_total_chk} before_exp={cur_exp_raw} after_exp={new_exp}")
                                except Exception:
                                    pass
                                grew_total = (updated['totalGB'] > cur_total) if (updated['totalGB'] != cur_total) else (add_gb == 0)
                                grew_exp = (updated['expiryTime'] > cur_exp_raw) if (updated['expiryTime'] != cur_exp_raw) else (add_days == 0)
                                if new_total_chk == updated['totalGB'] and (new_exp == updated['expiryTime'] or abs(new_exp - updated['expiryTime']) <= 5) and (grew_total or grew_exp):
                                    return updated, "Success"
                    last_ep = ep; last_code = r.status_code; last_err = f"HTTP {r.status_code}: {(r.text or '')[:160]}"
                except requests.RequestException as e:
                    last_ep = ep; last_code = None; last_err = str(e)
                    continue
            # Fallback: update full inbound (some versions require full object)
            try:
                up_paths = [
                    f"/panel/api/inbounds/update/{int(inbound_id)}",
                    f"/xui/api/inbounds/update/{int(inbound_id)}",
                    f"/panel/API/inbounds/update/{int(inbound_id)}",
                    f"/xui/API/inbounds/update/{int(inbound_id)}",
                ]
                full = self._fetch_inbound_detail(inbound_id) or {}
                # embed updated client back
                try:
                    cur_settings = json.loads(full.get('settings')) if isinstance(full.get('settings'), str) else (full.get('settings') or {})
                except Exception:
                    cur_settings = {}
                cur_clients = list(cur_settings.get('clients') or [])
                for i, cc in enumerate(cur_clients):
                    if cc.get('email') == username:
                        cur_clients[i] = updated
                        break
                else:
                    cur_clients.append(updated)
                cur_settings['clients'] = cur_clients
                settings_payload_str = json.dumps(cur_settings)
                full_payload = {
                    "id": int(inbound_id),
                    "up": full.get('up', 0),
                    "down": full.get('down', 0),
                    "total": full.get('total', 0),
                    "remark": full.get('remark') or "",
                    "enable": bool(full.get('enable', True)),
                    "expiryTime": full.get('expiryTime', 0) or 0,
                    "listen": full.get('listen') or "",
                    "port": full.get('port') or 0,
                    "protocol": full.get('protocol') or full.get('type') or "vless",
                    "settings": settings_payload_str,
                    "streamSettings": full.get('streamSettings') or full.get('stream_settings') or "{}",
                    "sniffing": full.get('sniffing') or "{}",
                    "allocate": full.get('allocate') or "{}",
                }
                for p in up_paths:
                    try:
                        rr = self.session.post(f"{self.base_url}{p}", headers=json_headers, json=full_payload, timeout=15)
                        if rr.status_code in (200, 201):
                            # verify
                            ref2 = self._fetch_inbound_detail(inbound_id)
                            try:
                                robj2 = json.loads(ref2.get('settings')) if isinstance(ref2.get('settings'), str) else (ref2.get('settings') or {})
                            except Exception:
                                robj2 = {}
                            for c2 in (robj2.get('clients') or []):
                                if c2.get('email') == username and int(c2.get('expiryTime', 0) or 0) == updated['expiryTime'] and int(c2.get('totalGB', 0) or 0) == updated['totalGB']:
                                    return updated, "Success"
                        else:
                            last_ep = p; last_code = rr.status_code; last_err = f"{p} -> HTTP {rr.status_code}: {(rr.text or '')[:160]}"
                    except requests.RequestException as e2:
                        last_ep = p; last_code = None; last_err = f"{p} -> EXC {e2}"
            except Exception as e3:
                last_ep = 'fallback'; last_code = None; last_err = f"fallback error: {e3}"
            try:
                logger.error(f"X-UI renew failed for {username} on inbound {inbound_id}: endpoint={last_ep} status={last_code} detail={last_err}")
            except Exception:
                pass
            return None, (last_err or "به‌روزرسانی کلاینت ناموفق بود")
        except Exception as e:
            return None, str(e)

    def renew_by_recreate_on_inbound(self, inbound_id: int, username: str, add_gb: float, add_days: int):
        if not self.get_token():
            return None, "خطا در ورود به پنل X-UI"
        inbound = self._fetch_inbound_detail(inbound_id)
        if not inbound:
            return None, "اینباند یافت نشد"
        try:
            import json as _json, uuid as _uuid, random as _rand, string as _str
            now_ms = int(datetime.now().timestamp() * 1000)
            settings_str = inbound.get('settings')
            settings_obj = _json.loads(settings_str) if isinstance(settings_str, str) else (settings_str or {})
            clients = settings_obj.get('clients') or []
            if not isinstance(clients, list):
                return None, "ساختار کلاینت‌ها نامعتبر است"
            old = None
            for c in clients:
                if c.get('email') == username:
                    old = c
                    break
            if not old:
                return None, "کلاینت یافت نشد"
            current_exp = int(old.get('expiryTime', 0) or 0)
            is_ms = current_exp > 10**11
            now_unit = now_ms if is_ms else int(now_ms / 1000)
            add_unit = (int(add_days) * 86400 * (1000 if is_ms else 1)) if add_days and int(add_days) > 0 else 0
            base = max(current_exp, now_unit)
            target_exp = base + add_unit if add_unit > 0 else current_exp
            add_bytes = int(float(add_gb) * (1024 ** 3)) if add_gb and add_gb > 0 else 0
            cur_total = int(old.get('totalGB', 0) or 0)
            new_total = cur_total + (add_bytes if add_bytes > 0 else 0)
            # delete old
            old_uuid = old.get('id') or old.get('uuid') or ''
            del_eps = [
                f"{self.base_url}/panel/api/inbounds/delClient",
                f"{self.base_url}/xui/api/inbounds/delClient",
                f"{self.base_url}/panel/API/inbounds/delClient",
                f"{self.base_url}/xui/API/inbounds/delClient",
                f"{self.base_url}/xui/api/inbound/delClient",
            ]
            del_eps = ([f"{e}/{old_uuid}" for e in del_eps] + del_eps) if old_uuid else del_eps
            headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
            for ep in del_eps:
                try:
                    for body in (
                        {"id": int(inbound_id), "clientId": old_uuid},
                        {"id": int(inbound_id), "uuid": old_uuid},
                        {"id": int(inbound_id), "email": username},
                    ):
                        r = self.session.post(ep, headers=headers, json=body, timeout=12)
                        if r.status_code in (200, 201):
                            break
                except requests.RequestException:
                    continue
            # add new
            new_client = {
                "id": str(_uuid.uuid4()),
                "email": username,
                "totalGB": new_total,
                "expiryTime": target_exp,
                "enable": True,
                "limitIp": int(old.get('limitIp', 0) or 0),
                "subId": ''.join(_rand.choices(_str.ascii_lowercase + _str.digits, k=12)),
                "reset": 0
            }
            add_eps = [
                f"{self.base_url}/panel/api/inbounds/addClient",
                f"{self.base_url}/xui/api/inbounds/addClient",
                f"{self.base_url}/panel/API/inbounds/addClient",
                f"{self.base_url}/xui/API/inbounds/addClient",
                f"{self.base_url}/xui/api/inbound/addClient",
            ]
            json_headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
            form_headers = {'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}
            settings_payload = _json.dumps({"clients": [new_client]})
            for ep in add_eps:
                try:
                    r1 = self.session.post(ep, headers=json_headers, json={"id": int(inbound_id), "clients": [new_client]}, timeout=15)
                    if r1.status_code in (200, 201):
                        return new_client, "Success"
                    r2 = self.session.post(ep, headers=json_headers, json={"id": int(inbound_id), "settings": settings_payload}, timeout=15)
                    if r2.status_code in (200, 201):
                        return new_client, "Success"
                    r3 = self.session.post(ep, headers=form_headers, data={"id": str(int(inbound_id)), "settings": settings_payload}, timeout=15)
                    if r3.status_code in (200, 201):
                        return new_client, "Success"
                except requests.RequestException:
                    continue
            return None, "ساخت کلاینت جدید ناموفق بود"
        except Exception as e:
            return None, str(e)

    def get_configs_for_user_on_inbound(self, inbound_id: int, username: str, preferred_id: str = None) -> list:
        inbound = self._fetch_inbound_detail(inbound_id)
        if not inbound:
            return []
        # helper to find client
        def _find_client(inv):
            s = inv.get('settings')
            try:
                obj = json.loads(s) if isinstance(s, str) else (s or {})
            except Exception:
                obj = {}
            chosen = None
            for c in (obj.get('clients') or []):
                if preferred_id and (c.get('id') == preferred_id or c.get('uuid') == preferred_id):
                    return c
                if c.get('email') == username and chosen is None:
                    chosen = c
            return chosen
        client = _find_client(inbound)
        # small retry to allow propagation
        retries = 2
        while client is None and retries > 0:
            _time.sleep(0.7)
            inbound = self._fetch_inbound_detail(inbound_id)
            if not inbound:
                break
            client = _find_client(inbound)
            retries -= 1
        if not client:
            return []
        try:
            settings_str = inbound.get('settings')
            settings_obj = json.loads(settings_str) if isinstance(settings_str, str) else (settings_str or {})
            proto = (inbound.get('protocol') or '').lower()
            port = inbound.get('port') or inbound.get('listen_port') or 0
            stream_raw = inbound.get('streamSettings') or inbound.get('stream_settings')
            stream = json.loads(stream_raw) if isinstance(stream_raw, str) else (stream_raw or {})
            network = (stream.get('network') or '').lower() or 'tcp'
            security = (stream.get('security') or '').lower() or ''
            sni = ''
            if security == 'tls':
                tls = stream.get('tlsSettings') or {}
                sni = tls.get('serverName') or ''
            elif security == 'reality':
                reality = stream.get('realitySettings') or {}
                sni = (reality.get('serverNames') or [''])[0]
            path = ''
            host_header = ''
            service_name = ''
            header_type = ''
            if network == 'ws':
                ws = stream.get('wsSettings') or {}
                path = ws.get('path') or '/'
                headers = ws.get('headers') or {}
                host_header = headers.get('Host') or headers.get('host') or ''
            elif network == 'tcp':
                tcp = stream.get('tcpSettings') or {}
                header = tcp.get('header') or {}
                if (header.get('type') or '').lower() == 'http':
                    header_type = 'http'
                    req = header.get('request') or {}
                    rp = req.get('path')
                    if isinstance(rp, list) and rp:
                        path = rp[0] or '/'
                    elif isinstance(rp, str) and rp:
                        path = rp
                    else:
                        path = '/'
                    h = req.get('headers') or {}
                    hh = h.get('Host') or h.get('host') or ''
                    if isinstance(hh, list) and hh:
                        host_header = hh[0]
                    elif isinstance(hh, str):
                        host_header = hh
            if network == 'grpc':
                grpc = stream.get('grpcSettings') or {}
                service_name = grpc.get('serviceName') or ''
            # host
            parts = urlsplit(self.base_url)
            host = parts.hostname or ''
            if not host:
                host = host_header or sni or host
            uuid_val = client.get('id') or client.get('uuid') or ''
            passwd = client.get('password') or ''
            name = username
            configs = []
            if proto == 'vless' and uuid_val:
                qs = []
                if network:
                    qs.append(f'type={network}')
                if network == 'ws':
                    if path:
                        qs.append(f'path={path}')
                    if host_header:
                        qs.append(f'host={host_header}')
                if network == 'tcp' and header_type == 'http':
                    qs.append('headerType=http')
                    if path:
                        qs.append(f'path={path}')
                    if host_header:
                        qs.append(f'host={host_header}')
                if network == 'grpc' and service_name:
                    qs.append(f'serviceName={service_name}')
                if security:
                    qs.append(f'security={security}')
                    if sni:
                        qs.append(f'sni={sni}')
                else:
                    qs.append('security=none')
                flow = client.get('flow')
                if flow:
                    qs.append(f'flow={flow}')
                query = '&'.join(qs)
                uri = f"vless://{uuid_val}@{host}:{port}?{query}#{name}"
                configs.append(uri)
            elif proto == 'vmess' and uuid_val:
                vm = {
                    "v": "2",
                    "ps": name,
                    "add": host,
                    "port": str(port),
                    "id": uuid_val,
                    "aid": "0",
                    "net": network,
                    "type": "none",
                    "host": host_header or sni or host,
                    "path": path or "/",
                    "tls": "tls" if security in ("tls","reality") else "",
                    "sni": sni or ""
                }
                import base64 as _b64
                b = _b64.b64encode(json.dumps(vm, ensure_ascii=False).encode('utf-8')).decode('utf-8')
                configs.append(f"vmess://{b}")
            elif proto == 'trojan' and passwd:
                qs = []
                if network:
                    qs.append(f'type={network}')
                if network == 'ws':
                    if path:
                        qs.append(f'path={path}')
                    if host_header:
                        qs.append(f'host={host_header}')
                if network == 'grpc' and service_name:
                    qs.append(f'serviceName={service_name}')
                if security:
                    qs.append(f'security={security}')
                    if sni:
                        qs.append(f'sni={sni}')
                else:
                    qs.append('security=none')
                query = '&'.join(qs)
                uri = f"trojan://{passwd}@{host}:{port}?{query}#{name}"
                configs.append(uri)
            return configs
        except Exception:
            return []

    def recreate_user_key_on_inbound(self, inbound_id: int, username: str):
        # Login and fetch inbound
        if not self.get_token():
            return None
        inbound = self._fetch_inbound_detail(inbound_id)
        if not inbound:
            return None
        try:
            import random as _rand, string as _str
            settings_str = inbound.get('settings')
            settings_obj = json.loads(settings_str) if isinstance(settings_str, str) else (settings_str or {})
            clients = settings_obj.get('clients') or []
            if not isinstance(clients, list):
                return None
            # locate old client
            old_client = None
            for c in clients:
                if c.get('email') == username:
                    old_client = c
                    break
            if not old_client:
                return None
            # prepare new client preserving quota and expiry
            # Build new email by keeping prefix and changing suffix
            if '_' in username:
                base_prefix = username.rsplit('_', 1)[0]
            else:
                base_prefix = username
            new_email = f"{base_prefix}_{uuid.uuid4().hex[:6]}"
            new_client = {
                "id": str(uuid.uuid4()),
                "email": new_email,
                "totalGB": int(old_client.get('totalGB', 0) or 0),
                "expiryTime": int(old_client.get('expiryTime', 0) or 0),
                "enable": True,
                "limitIp": int(old_client.get('limitIp', 0) or 0),
                "subId": ''.join(_rand.choices(_str.ascii_lowercase + _str.digits, k=12)),
                "reset": 0,
            }
            # Try to add new client first
            add_endpoints = [
                f"{self.base_url}/xui/API/inbounds/addClient",
                f"{self.base_url}/panel/API/inbounds/addClient",
                f"{self.base_url}/xui/api/inbounds/addClient",
                f"{self.base_url}/panel/api/inbounds/addClient",
                f"{self.base_url}/xui/api/inbound/addClient",
            ]
            json_headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
            form_headers = {'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'}
            added = False
            last_err = None
            for ep in add_endpoints:
                try:
                    # A) clients array JSON
                    r1 = self.session.post(ep, headers=json_headers, json={"id": int(inbound_id), "clients": [new_client]}, timeout=15)
                    if r1.status_code in (200, 201):
                        added = True
                        break
                    # B) settings JSON string
                    settings_payload = json.dumps({"clients": [new_client]})
                    r2 = self.session.post(ep, headers=json_headers, json={"id": int(inbound_id), "settings": settings_payload}, timeout=15)
                    if r2.status_code in (200, 201):
                        added = True
                        break
                    # C) form urlencoded
                    r3 = self.session.post(ep, headers=form_headers, data={"id": str(int(inbound_id)), "settings": settings_payload}, timeout=15)
                    if r3.status_code in (200, 201):
                        added = True
                        break
                    last_err = f"{ep} -> HTTP {r1.status_code}/{r2.status_code}/{r3.status_code}"
                except requests.RequestException as e:
                    last_err = str(e)
                    continue
            if not added:
                return None
            # Delete old client (best-effort)
            old_uuid = old_client.get('id') or old_client.get('uuid') or ''
            del_endpoints = [
                f"{self.base_url}/xui/API/inbounds/delClient",
                f"{self.base_url}/panel/API/inbounds/delClient",
                f"{self.base_url}/xui/api/inbounds/delClient",
                f"{self.base_url}/panel/api/inbounds/delClient",
                f"{self.base_url}/xui/api/inbound/delClient",
            ]
            del_endpoints = ([f"{e}/{old_uuid}" for e in del_endpoints] + del_endpoints) if old_uuid else del_endpoints
            for ep in del_endpoints:
                try:
                    payloads = [
                        {"id": int(inbound_id), "clientId": old_uuid},
                        {"id": int(inbound_id), "uuid": old_uuid},
                        {"id": int(inbound_id), "email": username},
                    ]
                    ok = False
                    for p in payloads:
                        r = self.session.post(ep, headers=json_headers, json=p, timeout=12)
                        if r.status_code in (200, 201):
                            ok = True
                            break
                    if ok:
                        break
                except requests.RequestException:
                    continue
            return new_client
        except Exception:
            return None


class ThreeXuiAPI(BasePanelAPI):
    """3x-UI support using lowercase /xui/api endpoints."""

    def __init__(self, panel_row):
        self.panel_id = panel_row['id']
        self.base_url = panel_row['url'].rstrip('/')
        self.username = panel_row['username']
        self.password = panel_row['password']
        self.sub_base = (panel_row.get('sub_base') or '').strip().rstrip('/') if isinstance(panel_row, dict) else ''
        self.session = requests.Session()
        self._json_headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
        }

    def get_token(self):
        # Try form login first (more compatible)
        try:
            try:
                self.session.get(f"{self.base_url}/login", timeout=8)
            except requests.RequestException:
                pass
            form_headers = {
                'Accept': 'text/html,application/json',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
            }
            resp = self.session.post(
                f"{self.base_url}/login",
                data={"username": self.username, "password": self.password},
                headers=form_headers,
                allow_redirects=False,
                timeout=12,
            )
            if resp.status_code in (200, 204, 302, 303):
                return True
        except requests.RequestException:
            pass
        # Fallback to JSON login
        try:
            resp = self.session.post(
                f"{self.base_url}/login",
                json={"username": self.username, "password": self.password},
                headers=self._json_headers,
                timeout=12,
            )
            if resp.status_code in (200, 204, 302, 303):
                return True
        except requests.RequestException as e:
            logger.error(f"3x-UI login error: {e}")
        return False

    def list_inbounds(self):
        if not self.get_token():
            return None, "خطا در ورود به پنل 3x-UI"
        try:
            resp = self.session.get(
                f"{self.base_url}/xui/API/inbounds/",
                headers={'Accept': 'application/json'},
                timeout=10,
            )
            if resp.status_code != 200:
                return None, f"HTTP {resp.status_code}"
            data = resp.json()
            items = data.get('obj') if isinstance(data, dict) else data
            if not isinstance(items, list):
                return None, "لیست اینباند نامعتبر است"
            inbounds = []
            for it in items:
                inbounds.append({
                    'id': it.get('id'),
                    'remark': it.get('remark') or it.get('tag') or str(it.get('id')),
                    'protocol': it.get('protocol') or it.get('type') or 'unknown',
                    'port': it.get('port') or it.get('listen_port') or 0,
                })
            return inbounds, "Success"
        except requests.RequestException as e:
            logger.error(f"3x-UI list_inbounds error: {e}")
            return None, str(e)
        except ValueError as ve:
            logger.error(f"3x-UI JSON parse error for /xui/API/inbounds/: {ve}")
            return None, "JSON parse error"

    def create_user_on_inbound(self, inbound_id: int, user_id: int, plan):
        if not self.get_token():
            return None, None, "خطا در ورود به پنل 3x-UI"
        try:
            new_username = f"user_{user_id}_{uuid.uuid4().hex[:6]}"
            import random, string
            subid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
            try:
                traffic_gb = float(plan['traffic_gb'])
            except Exception:
                traffic_gb = 0.0
            total_bytes = int(traffic_gb * (1024 ** 3)) if traffic_gb > 0 else 0
            try:
                days = int(plan['duration_days'])
                expiry_ms = int((datetime.now() + timedelta(days=days)).timestamp() * 1000) if days > 0 else 0
            except Exception:
                expiry_ms = 0

            new_client_id = str(uuid.uuid4())
            client_obj = {
                "id": new_client_id,
                "email": new_username,
                "totalGB": total_bytes,
                "expiryTime": expiry_ms,
                "enable": True,
                "limitIp": 0,
                "subId": subid,
                "reset": 0
            }

            def _is_success(json_obj):
                if not isinstance(json_obj, dict):
                    return False
                if json_obj.get('success') is True:
                    return True
                status_val = str(json_obj.get('status', '')).lower()
                if status_val in ('ok', 'success', '200'):
                    return True
                code_val = str(json_obj.get('code', ''))
                if code_val.startswith('2'):
                    return True
                msg_val = json_obj.get('msg') or json_obj.get('message') or ''
                if isinstance(msg_val, str) and ('success' in msg_val.lower() or 'ok' in msg_val.lower()):
                    return True
                return False

            endpoints = [
                f"{self.base_url}/xui/api/inbounds/addClient",
                f"{self.base_url}/panel/api/inbounds/addClient",
                f"{self.base_url}/xui/api/inbound/addClient",
            ]

            # Try each endpoint with multiple payload formats
            last_preview = None
            for ep in endpoints:
                # 1) clients array JSON
                payload1 = {"id": int(inbound_id), "clients": [client_obj]}
                r1 = self.session.post(ep, headers=self._json_headers, json=payload1, timeout=15)
                if r1.status_code in (200, 201):
                    try:
                        j1 = r1.json()
                    except ValueError:
                        j1 = {}
                    if _is_success(j1):
                        chosen_ep = ep
                        break
                    last_preview = f"endpoint={ep} form=clients preview={(r1.text or '')[:200]}"
                else:
                    last_preview = f"endpoint={ep} form=clients HTTP {r1.status_code}: {(r1.text or '')[:200]}"
                # 2) settings JSON string
                settings_obj = {"clients": [client_obj]}
                payload2 = {"id": int(inbound_id), "settings": json.dumps(settings_obj)}
                r2 = self.session.post(ep, headers=self._json_headers, json=payload2, timeout=15)
                if r2.status_code in (200, 201):
                    try:
                        j2 = r2.json()
                    except ValueError:
                        j2 = {}
                    if _is_success(j2):
                        chosen_ep = ep
                        break
                    last_preview = f"endpoint={ep} form=settings preview={(r2.text or '')[:200]}"
                else:
                    last_preview = f"endpoint={ep} form=settings HTTP {r2.status_code}: {(r2.text or '')[:200]}"
                # 3) form-urlencoded with settings
                form_headers = {
                    'Accept': 'application/json',
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'X-Requested-With': 'XMLHttpRequest',
                }
                r3 = self.session.post(ep, headers=form_headers, data={'id': str(int(inbound_id)), 'settings': json.dumps(settings_obj)}, timeout=15)
                if r3.status_code in (200, 201):
                    try:
                        j3 = r3.json()
                    except ValueError:
                        j3 = {}
                    if _is_success(j3):
                        chosen_ep = ep
                        break
                    last_preview = f"endpoint={ep} form=form preview={(r3.text or '')[:200]}"
                else:
                    last_preview = f"endpoint={ep} form=form HTTP {r3.status_code}: {(r3.text or '')[:200]}"
            else:
                # no break -> all failed
                return None, None, f"API failure: {last_preview or 'unknown'}"

            # Success path
            if self.sub_base:
                origin = self.sub_base
            else:
                parts = urlsplit(self.base_url)
                host = parts.hostname or ''
                port = ''
                if parts.port and not ((parts.scheme == 'http' and parts.port == 80) or (parts.scheme == 'https' and parts.port == 443)):
                    port = f":{parts.port}"
                origin = f"{parts.scheme}://{host}{port}"
            sub_link = f"{origin}/sub/{subid}"
            # Return username and also stash client id for downstream if needed (admin layer persists inbound id)
            return new_username, sub_link, "Success"
        except requests.RequestException as e:
            logger.error(f"3x-UI create_user_on_inbound error: {e}")
            return None, None, str(e)

    async def get_all_users(self):
        return None, "Not supported for 3x-UI"

    async def get_user(self, username):
        if not self.get_token():
            return None, "خطا در ورود به پنل 3x-UI"
        inbounds, msg = self.list_inbounds()
        if not inbounds:
            return None, msg
        for ib in inbounds:
            inbound_id = ib.get('id')
            inbound = self._fetch_inbound_detail(inbound_id)
            if not inbound:
                continue
            settings_str = inbound.get('settings')
            try:
                settings_obj = json.loads(settings_str) if isinstance(settings_str, str) else {}
            except Exception:
                settings_obj = {}
            clients = settings_obj.get('clients') or []
            if not isinstance(clients, list):
                continue
            for c in clients:
                if c.get('email') == username:
                    total_bytes = int(c.get('totalGB', 0) or 0)
                    expiry_ms = int(c.get('expiryTime', 0) or 0)
                    expire = int(expiry_ms / 1000) if expiry_ms > 0 else 0
                    subid = c.get('subId') or ''
                    if self.sub_base:
                        origin = self.sub_base
                    else:
                        parts = urlsplit(self.base_url)
                        host = parts.hostname or ''
                        port = ''
                        if parts.port and not ((parts.scheme == 'http' and parts.port == 80) or (parts.scheme == 'https' and parts.port == 443)):
                            port = f":{parts.port}"
                        origin = f"{parts.scheme}://{host}{port}"
                    sub_link = f"{origin}/sub/{subid}" if subid else ''
                    return {
                        'data_limit': total_bytes,
                        'used_traffic': 0,
                        'expire': expire,
                        'subscription_url': sub_link,
                    }, "Success"
        return None, "کاربر یافت نشد"

    def _fetch_inbound_detail(self, inbound_id: int):
        paths = [
            f"/xui/api/inbounds/get/{inbound_id}",
            f"/panel/api/inbounds/get/{inbound_id}",
            f"/xui/inbounds/get/{inbound_id}",
            f"/panel/inbounds/get/{inbound_id}",
            f"/xui/API/inbounds/get/{inbound_id}",
            f"/panel/API/inbounds/get/{inbound_id}",
        ]
        for p in paths:
            try:
                resp = self.session.get(f"{self.base_url}{p}", headers={'Accept': 'application/json'}, timeout=12)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                inbound = data.get('obj') if isinstance(data, dict) else data
                if isinstance(inbound, dict):
                    return inbound
            except Exception:
                continue
        return None

    def get_configs_for_user_on_inbound(self, inbound_id: int, username: str, preferred_id: str = None) -> list:
        inbound = self._fetch_inbound_detail(inbound_id)
        if not inbound:
            return []
        # Retry a little to ensure client appears
        def _find_client(inv):
            s = inv.get('settings')
            try:
                obj = json.loads(s) if isinstance(s, str) else (s or {})
            except Exception:
                obj = {}
            chosen = None
            for c in (obj.get('clients') or []):
                if preferred_id and (c.get('id') == preferred_id or c.get('uuid') == preferred_id):
                    return c
                if c.get('email') == username and chosen is None:
                    chosen = c
            return chosen
        client = _find_client(inbound)
        retries = 2
        while client is None and retries > 0:
            _time.sleep(0.7)
            inbound = self._fetch_inbound_detail(inbound_id)
            if not inbound:
                break
            client = _find_client(inbound)
            retries -= 1
        if not client:
            return []
        try:
            settings_str = inbound.get('settings')
            settings_obj = json.loads(settings_str) if isinstance(settings_str, str) else (settings_str or {})
            proto = (inbound.get('protocol') or '').lower()
            port = inbound.get('port') or inbound.get('listen_port') or 0
            stream_raw = inbound.get('streamSettings') or inbound.get('stream_settings')
            stream = json.loads(stream_raw) if isinstance(stream_raw, str) else (stream_raw or {})
            network = (stream.get('network') or '').lower() or 'tcp'
            security = (stream.get('security') or '').lower() or ''
            sni = ''
            if security == 'tls':
                tls = stream.get('tlsSettings') or {}
                sni = tls.get('serverName') or ''
            elif security == 'reality':
                reality = stream.get('realitySettings') or {}
                sni = (reality.get('serverNames') or [''])[0]
            path = ''
            host_header = ''
            service_name = ''
            header_type = ''
            if network == 'ws':
                ws = stream.get('wsSettings') or {}
                path = ws.get('path') or '/'
                headers = ws.get('headers') or {}
                host_header = headers.get('Host') or headers.get('host') or ''
            elif network == 'tcp':
                tcp = stream.get('tcpSettings') or {}
                header = tcp.get('header') or {}
                if (header.get('type') or '').lower() == 'http':
                    header_type = 'http'
                    req = header.get('request') or {}
                    rp = req.get('path')
                    if isinstance(rp, list) and rp:
                        path = rp[0] or '/'
                    elif isinstance(rp, str) and rp:
                        path = rp
                    else:
                        path = '/'
                    h = req.get('headers') or {}
                    hh = h.get('Host') or h.get('host') or ''
                    if isinstance(hh, list) and hh:
                        host_header = hh[0]
                    elif isinstance(hh, str):
                        host_header = hh
            if network == 'grpc':
                grpc = stream.get('grpcSettings') or {}
                service_name = grpc.get('serviceName') or ''
            from urllib.parse import urlsplit as _us
            parts = _us(getattr(self, 'sub_base', '') or self.base_url)
            host = parts.hostname or ''
            if not host:
                host = host_header or sni or host
            uuid = client.get('id') or client.get('uuid') or ''
            passwd = client.get('password') or ''
            name = username
            configs = []
            if proto == 'vless' and uuid:
                qs = []
                if network:
                    qs.append(f'type={network}')
                if network == 'ws':
                    if path:
                        qs.append(f'path={path}')
                    if host_header:
                        qs.append(f'host={host_header}')
                if network == 'tcp' and header_type == 'http':
                    qs.append('headerType=http')
                    if path:
                        qs.append(f'path={path}')
                    if host_header:
                        qs.append(f'host={host_header}')
                if network == 'grpc' and service_name:
                    qs.append(f'serviceName={service_name}')
                if security:
                    qs.append(f'security={security}')
                    if sni:
                        qs.append(f'sni={sni}')
                else:
                    qs.append('security=none')
                flow = client.get('flow')
                if flow:
                    qs.append(f'flow={flow}')
                query = '&'.join(qs)
                uri = f"vless://{uuid}@{host}:{port}?{query}#{name}"
                configs.append(uri)
            elif proto == 'vmess' and uuid:
                vm = {
                    "v": "2",
                    "ps": name,
                    "add": host,
                    "port": str(port),
                    "id": uuid,
                    "aid": "0",
                    "net": network,
                    "type": "none",
                    "host": host_header or sni or host,
                    "path": path or "/",
                    "tls": "tls" if security in ("tls","reality") else "",
                    "sni": sni or ""
                }
                import base64 as _b64
                b = _b64.b64encode(json.dumps(vm, ensure_ascii=False).encode('utf-8')).decode('utf-8')
                configs.append(f"vmess://{b}")
            elif proto == 'trojan' and passwd:
                qs = []
                if network:
                    qs.append(f'type={network}')
                if network == 'ws':
                    if path:
                        qs.append(f'path={path}')
                    if host_header:
                        qs.append(f'host={host_header}')
                if network == 'grpc' and service_name:
                    qs.append(f'serviceName={service_name}')
                if security:
                    qs.append(f'security={security}')
                    if sni:
                        qs.append(f'sni={sni}')
                query = '&'.join(qs)
                uri = f"trojan://{passwd}@{host}:{port}?{query}#{name}"
                configs.append(uri)
            return configs
        except Exception:
            return []

    def renew_by_recreate_on_inbound(self, inbound_id: int, username: str, add_gb: float, add_days: int):
        # Delete old client and create a new one with increased quota/expiry
        if not self.get_token():
            return None, "خطا در ورود به پنل 3x-UI"
        inbound = self._fetch_inbound_detail(inbound_id)
        if not inbound:
            return None, "اینباند یافت نشد"
        try:
            import json as _json, uuid as _uuid, random as _rand, string as _str
            now_ms = int(datetime.now().timestamp() * 1000)
            settings_str = inbound.get('settings')
            settings_obj = _json.loads(settings_str) if isinstance(settings_str, str) else (settings_str or {})
            clients = settings_obj.get('clients') or []
            if not isinstance(clients, list):
                return None, "ساختار کلاینت‌ها نامعتبر است"
            old = None
            for c in clients:
                if c.get('email') == username:
                    old = c
                    break
            if not old:
                return None, "کلاینت یافت نشد"
            add_bytes = int(float(add_gb) * (1024 ** 3)) if add_gb and add_gb > 0 else 0
            cur_total = int(old.get('totalGB', 0) or 0)
            cur_exp = int(old.get('expiryTime', 0) or 0)
            # Detect seconds vs milliseconds for expiry
            is_ms = cur_exp > 10**11
            now_unit = now_ms if is_ms else int(now_ms / 1000)
            add_unit = (int(add_days) * 86400 * (1000 if is_ms else 1)) if add_days and int(add_days) > 0 else 0
            base = max(cur_exp, now_unit)
            target_exp = base + add_unit if add_unit > 0 else cur_exp
            new_total = cur_total + (add_bytes if add_bytes > 0 else 0)
            # Delete old client first
            old_uuid = old.get('id') or old.get('uuid') or ''
            del_endpoints = [
                f"{self.base_url}/xui/api/inbounds/delClient",
                f"{self.base_url}/panel/api/inbounds/delClient",
                f"{self.base_url}/xui/API/inbounds/delClient",
                f"{self.base_url}/panel/API/inbounds/delClient",
                f"{self.base_url}/xui/api/inbound/delClient",
            ]
            if old_uuid:
                del_endpoints = ([f"{e}/{old_uuid}" for e in del_endpoints] + del_endpoints)
            json_headers = {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}
            deleted = False
            for ep in del_endpoints:
                try:
                    # try by id/uuid/email
                    for body in (
                        {"id": int(inbound_id), "clientId": old_uuid},
                        {"id": int(inbound_id), "uuid": old_uuid},
                        {"id": int(inbound_id), "email": username},
                    ):
                        r = self.session.post(ep, headers=json_headers, json=body, timeout=12)
                        if r.status_code in (200, 201):
                            deleted = True
                            break
                    if deleted:
                        break
                except requests.RequestException:
                    continue
            if not deleted:
                # Proceed anyway to recreate (panel may auto-replace)
                pass
            # Create new client with increased limits
            new_client = {
                "id": str(_uuid.uuid4()),
                "email": username,
                "totalGB": new_total,
                "expiryTime": target_exp,
                "enable": True,
                "limitIp": int(old.get('limitIp', 0) or 0),
                "subId": ''.join(_rand.choices(_str.ascii_lowercase + _str.digits, k=12)),
                "reset": 0,
                "alterId": int(old.get('alterId', 0) or 0)
            }
            add_endpoints = [
                f"{self.base_url}/xui/api/inbounds/addClient",
                f"{self.base_url}/panel/api/inbounds/addClient",
                f"{self.base_url}/xui/API/inbounds/addClient",
                f"{self.base_url}/panel/API/inbounds/addClient",
                f"{self.base_url}/xui/api/inbound/addClient",
            ]
            form_headers = {
                'Accept': 'application/json',
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest',
            }
            added = False; last_err = None; last_ep = None
            settings_payload = _json.dumps({"clients": [new_client]})
            for ep in add_endpoints:
                try:
                    # A) JSON clients array
                    r1 = self.session.post(ep, headers=json_headers, json={"id": int(inbound_id), "clients": [new_client]}, timeout=15)
                    if r1.status_code in (200, 201):
                        added = True; last_ep = ep; break
                    # B) JSON settings string
                    r2 = self.session.post(ep, headers=json_headers, json={"id": int(inbound_id), "settings": settings_payload}, timeout=15)
                    if r2.status_code in (200, 201):
                        added = True; last_ep = ep; break
                    # C) form-urlencoded settings
                    r3 = self.session.post(ep, headers=form_headers, data={"id": str(int(inbound_id)), "settings": settings_payload}, timeout=15)
                    if r3.status_code in (200, 201):
                        added = True; last_ep = ep; break
                    last_err = f"{ep} -> HTTP {r1.status_code}/{r2.status_code}/{r3.status_code}"
                except requests.RequestException:
                    last_err = f"{ep} -> EXC"
                    continue
            if not added:
                return None, (last_err or "ساخت کلاینت جدید ناموفق بود")
            # Verify by refetching inbound
            ref = self._fetch_inbound_detail(inbound_id)
            try:
                robj = _json.loads(ref.get('settings')) if isinstance(ref.get('settings'), str) else (ref.get('settings') or {})
            except Exception:
                robj = {}
            for c2 in (robj.get('clients') or []):
                if c2.get('email') == username:
                    after_total = int(c2.get('totalGB', 0) or 0)
                    after_exp = int(c2.get('expiryTime', 0) or 0)
                    grew_total = after_total >= new_total
                    grew_exp = after_exp >= target_exp
                    if grew_total or grew_exp:
                        return new_client, "Success"
            try:
                logger.error(f"X-UI/3x-UI recreate verify failed: inbound={inbound_id} before_total={cur_total} after_total={after_total if 'after_total' in locals() else 'n/a'} before_exp={current_exp} after_exp={after_exp if 'after_exp' in locals() else 'n/a'} last_ep={last_ep} err={last_err}")
            except Exception:
                pass
            return None, "تایید افزایش حجم/زمان ناموفق بود"
        except Exception as e:
            return None, str(e)

    def renew_user_on_inbound(self, inbound_id: int, username: str, add_gb: float, add_days: int):
        # Ensure login
        try:
            self.get_token()
        except Exception:
            pass
        inbound = self._fetch_inbound_detail(inbound_id)
        if not inbound:
            return None, "اینباند یافت نشد"
        try:
            import json as _json
            now_ms = int(datetime.now().timestamp() * 1000)
            settings_str = inbound.get('settings')
            settings_obj = _json.loads(settings_str) if isinstance(settings_str, str) else (settings_str or {})
            clients = settings_obj.get('clients') or []
            if not isinstance(clients, list):
                return None, "ساختار کلاینت‌ها نامعتبر است"
            updated = None; idx = -1; old_uuid = None
            for i, c in enumerate(clients):
                if c.get('email') == username:
                    add_bytes = int(float(add_gb) * (1024 ** 3)) if add_gb and add_gb > 0 else 0
                    add_ms = (int(add_days) * 86400 * 1000) if add_days and int(add_days) > 0 else 0
                    current_exp = int(c.get('expiryTime', 0) or 0)
                    base = max(current_exp, now_ms)
                    target_exp = base + add_ms if add_ms > 0 else current_exp
                    new_total = int(c.get('totalGB', 0) or 0) + (add_bytes if add_bytes > 0 else 0)
                    updated = dict(c); idx = i; old_uuid = c.get('id') or c.get('uuid') or None
                    updated['expiryTime'] = target_exp
                    updated['totalGB'] = new_total
                    break
            if not updated:
                return None, "کلاینت یافت نشد"
            # Push full settings to ensure persistence
            full_clients = list(clients)
            if idx >= 0:
                full_clients[idx] = updated
            settings_obj['clients'] = full_clients
            payload_settings = _json.dumps(settings_obj)
            payload = {"id": int(inbound_id), "settings": payload_settings}
            # Try multiple endpoints
            base_endpoints = [
                "/xui/API/inbounds/updateClient",
                "/panel/API/inbounds/updateClient",
                "/xui/api/inbounds/updateClient",
                "/panel/api/inbounds/updateClient",
                "/xui/api/inbound/updateClient",
            ]
            endpoints = []
            if old_uuid:
                for be in base_endpoints:
                    if be.endswith('updateClient'):
                        endpoints.append(f"{be}/{old_uuid}")
            endpoints.extend(base_endpoints)
            last_preview = None
            for ep in endpoints:
                try:
                    r = self.session.post(f"{self.base_url}{ep}", headers={'Content-Type': 'application/json'}, json=payload, timeout=15)
                    if r.status_code in (200, 201):
                        try:
                            j = r.json()
                            # success detection
                            if isinstance(j, dict):
                                if j.get('success') is True:
                                    return updated, "Success"
                                status_val = str(j.get('status', '')).lower()
                                if status_val in ('ok','success','200'):
                                    return updated, "Success"
                                code_val = str(j.get('code', ''))
                                if code_val.startswith('2'):
                                    return updated, "Success"
                        except Exception:
                            # many 3x-ui return empty body on success; verify by reading back
                            new_ib = self._fetch_inbound_detail(inbound_id)
                            try:
                                ns = _json.loads(new_ib.get('settings')) if isinstance(new_ib.get('settings'), str) else (new_ib.get('settings') or {})
                            except Exception:
                                ns = {}
                            for c2 in (ns.get('clients') or []):
                                if c2.get('email') == username and int(c2.get('expiryTime', 0) or 0) == updated['expiryTime'] and int(c2.get('totalGB', 0) or 0) == updated['totalGB']:
                                    return updated, "Success"
                    else:
                        last_preview = f"{ep} -> HTTP {r.status_code}: {(r.text or '')[:180]}"
                except requests.RequestException:
                    last_preview = f"{ep} -> EXC"
                    continue
            if last_preview:
                try:
                    logger.error(f"3x-UI renew update failed: {last_preview}")
                except Exception:
                    pass
            return None, "به‌روزرسانی کلاینت ناموفق بود"
        except Exception as e:
            return None, str(e)

    async def renew_user_in_panel(self, username, plan):
        if not self.get_token():
            return None, "خطا در ورود به پنل 3x-UI"
        inbounds, msg = self.list_inbounds()
        if not inbounds:
            return None, msg
        now_ms = int(datetime.now().timestamp() * 1000)
        try:
            add_bytes = int(float(plan['traffic_gb']) * (1024 ** 3))
        except Exception:
            add_bytes = 0
        try:
            days = int(plan['duration_days'])
            add_ms = days * 86400 * 1000 if days > 0 else 0
        except Exception:
            add_ms = 0
        for ib in inbounds:
            inbound_id = ib.get('id')
            inbound = self._fetch_inbound_detail(inbound_id)
            if not inbound:
                continue
            settings_str = inbound.get('settings')
            clients = []
            try:
                if isinstance(settings_str, str):
                    settings_obj = json.loads(settings_str)
                    clients = settings_obj.get('clients', [])
            except Exception:
                clients = []
            if not isinstance(clients, list):
                continue
            for c in clients:
                if c.get('email') == username:
                    current_exp = int(c.get('expiryTime', 0) or 0)
                    base = max(current_exp, now_ms)
                    target_exp = base + (add_ms if add_ms > 0 else 0)
                    new_total = int(c.get('totalGB', 0) or 0) + (add_bytes if add_bytes > 0 else 0)
                    updated = dict(c)
                    updated['expiryTime'] = target_exp
                    updated['totalGB'] = new_total
                    settings_payload = json.dumps({"clients": [updated]})
                    payload = {"id": int(inbound_id), "settings": settings_payload}
                    for up in ["/xui/api/inbounds/updateClient", "/panel/api/inbounds/updateClient", "/xui/api/inbound/updateClient"]:
                        try:
                            resp = self.session.post(f"{self.base_url}{up}", headers={'Content-Type': 'application/json'}, json=payload, timeout=15)
                            if resp.status_code in (200, 201):
                                return updated, "Success"
                        except requests.RequestException:
                            continue
                    return None, "به‌روزرسانی کلاینت ناموفق بود"
        return None, "کلاینت برای تمدید یافت نشد"


class TxUiAPI(BasePanelAPI):
    """TX-UI support. Tries both tx and xui prefixes with lowercase endpoints. """

    def __init__(self, panel_row):
        self.panel_id = panel_row['id']
        self.base_url = panel_row['url'].rstrip('/')
        self.username = panel_row['username']
        self.password = panel_row['password']
        self.sub_base = (panel_row.get('sub_base') or '').strip().rstrip('/') if isinstance(panel_row, dict) else ''
        self.session = requests.Session()
        self._json_headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
        }

    def get_token(self):
        try:
            resp = self.session.post(
                f"{self.base_url}/login",
                json={"username": self.username, "password": self.password},
                headers=self._json_headers,
                timeout=12,
            )
            resp.raise_for_status()
            return True
        except requests.RequestException as e:
            logger.error(f"TX-UI login error: {e}")
            return False

    def list_inbounds(self):
        if not self.get_token():
            return None, "خطا در ورود به پنل TX-UI"
        try:
            endpoints = [
                f"{self.base_url}/tx/api/inbounds/list",
                f"{self.base_url}/xui/api/inbounds/list",
                f"{self.base_url}/tx/api/inbounds",
                f"{self.base_url}/xui/api/inbounds",
            ]
            last_error = None
            for attempt in range(2):
                for url in endpoints:
                    resp = self.session.get(url, headers=self._json_headers, timeout=12)
                    if resp.status_code != 200:
                        last_error = f"HTTP {resp.status_code}"
                        continue
                    ctype = resp.headers.get('content-type', '').lower()
                    body = resp.text or ''
                    if ('application/json' not in ctype) and not (body.strip().startswith('{') or body.strip().startswith('[')):
                        last_error = "پاسخ JSON معتبر نیست"
                        continue
                    try:
                        data = resp.json()
                    except ValueError as ve:
                        last_error = f"JSON parse error: {ve}"
                        continue
                    items = None
                    if isinstance(data, list):
                        items = data
                    elif isinstance(data, dict):
                        items = data.get('obj') if isinstance(data.get('obj'), list) else None
                        if items is None:
                            for _, v in data.items():
                                if isinstance(v, list):
                                    items = v
                                    break
                    if not isinstance(items, list):
                        last_error = "ساختار JSON لیست اینباند قابل تشخیص نیست"
                        continue
                    inbounds = []
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        inbounds.append({
                            'id': it.get('id'),
                            'remark': it.get('remark') or it.get('tag') or str(it.get('id')),
                            'protocol': it.get('protocol') or it.get('type') or 'unknown',
                            'port': it.get('port') or it.get('listen_port') or 0,
                        })
                    return inbounds, "Success"
                if attempt == 0:
                    self.get_token()
            if last_error:
                logger.error(f"TX-UI list_inbounds error: {last_error}")
                return None, last_error
            return None, "Unknown"
        except requests.RequestException as e:
            logger.error(f"TX-UI list_inbounds error: {e}")
            return None, str(e)

    def create_user_on_inbound(self, inbound_id: int, user_id: int, plan):
        if not self.get_token():
            return None, None, "خطا در ورود به پنل TX-UI"
        try:
            new_username = f"user_{user_id}_{uuid.uuid4().hex[:6]}"
            import random, string
            subid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
            try:
                traffic_gb = float(plan['traffic_gb'])
            except Exception:
                traffic_gb = 0.0
            total_bytes = int(traffic_gb * (1024 ** 3)) if traffic_gb > 0 else 0
            try:
                days = int(plan['duration_days'])
                expiry_ms = int((datetime.now() + timedelta(days=days)).timestamp() * 1000) if days > 0 else 0
            except Exception:
                expiry_ms = 0

            client_obj = {
                "id": str(uuid.uuid4()),
                "email": new_username,
                "totalGB": total_bytes,
                "expiryTime": expiry_ms,
                "enable": True,
                "limitIp": 0,
                "subId": subid,
                "reset": 0
            }

            def _is_success(json_obj):
                if not isinstance(json_obj, dict):
                    return False
                if json_obj.get('success') is True:
                    return True
                status_val = str(json_obj.get('status', '')).lower()
                if status_val in ('ok', 'success', '200'):
                    return True
                code_val = str(json_obj.get('code', ''))
                if code_val.startswith('2'):
                    return True
                msg_val = json_obj.get('msg') or json_obj.get('message') or ''
                if isinstance(msg_val, str) and ('success' in msg_val.lower() or 'ok' in msg_val.lower()):
                    return True
                return False

            endpoints = [
                f"{self.base_url}/tx/api/inbounds/addClient",
                f"{self.base_url}/xui/api/inbounds/addClient",
                f"{self.base_url}/panel/api/inbounds/addClient",
            ]

            last_preview = None
            for ep in endpoints:
                payload1 = {"id": int(inbound_id), "clients": [client_obj]}
                r1 = self.session.post(ep, headers=self._json_headers, json=payload1, timeout=15)
                if r1.status_code in (200, 201):
                    try:
                        j1 = r1.json()
                    except ValueError:
                        j1 = {}
                    if _is_success(j1):
                        chosen_ep = ep
                        break
                    last_preview = f"endpoint={ep} form=clients preview={(r1.text or '')[:200]}"
                else:
                    last_preview = f"endpoint={ep} form=clients HTTP {r1.status_code}: {(r1.text or '')[:200]}"
                settings_obj = {"clients": [client_obj]}
                payload2 = {"id": int(inbound_id), "settings": json.dumps(settings_obj)}
                r2 = self.session.post(ep, headers=self._json_headers, json=payload2, timeout=15)
                if r2.status_code in (200, 201):
                    try:
                        j2 = r2.json()
                    except ValueError:
                        j2 = {}
                    if _is_success(j2):
                        chosen_ep = ep
                        break
                    last_preview = f"endpoint={ep} form=settings preview={(r2.text or '')[:200]}"
                else:
                    last_preview = f"endpoint={ep} form=settings HTTP {r2.status_code}: {(r2.text or '')[:200]}"
                form_headers = {
                    'Accept': 'application/json',
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'X-Requested-With': 'XMLHttpRequest',
                }
                r3 = self.session.post(ep, headers=form_headers, data={'id': str(int(inbound_id)), 'settings': json.dumps(settings_obj)}, timeout=15)
                if r3.status_code in (200, 201):
                    try:
                        j3 = r3.json()
                    except ValueError:
                        j3 = {}
                    if _is_success(j3):
                        chosen_ep = ep
                        break
                    last_preview = f"endpoint={ep} form=form preview={(r3.text or '')[:200]}"
                else:
                    last_preview = f"endpoint={ep} form=form HTTP {r3.status_code}: {(r3.text or '')[:200]}"
            else:
                return None, None, f"API failure: {last_preview or 'unknown'}"

            if self.sub_base:
                origin = self.sub_base
            else:
                parts = urlsplit(self.base_url)
                host = parts.hostname or ''
                port = ''
                if parts.port and not ((parts.scheme == 'http' and parts.port == 80) or (parts.scheme == 'https' and parts.port == 443)):
                    port = f":{parts.port}"
                origin = f"{parts.scheme}://{host}{port}"
            # Default: no ?name=
            sub_link = f"{origin}/sub/{subid}"
            return new_username, sub_link, "Success"
        except requests.RequestException as e:
            logger.error(f"TX-UI create_user_on_inbound error: {e}")
            return None, None, str(e)

    async def get_all_users(self):
        return None, "Not supported for TX-UI"

    async def get_user(self, username):
        if not self.get_token():
            return None, "خطا در ورود به پنل TX-UI"
        inbounds, msg = self.list_inbounds()
        if not inbounds:
            return None, msg
        for ib in inbounds:
            inbound_id = ib.get('id')
            inbound = self._fetch_inbound_detail(inbound_id)
            if not inbound:
                continue
            settings_str = inbound.get('settings')
            try:
                settings_obj = json.loads(settings_str) if isinstance(settings_str, str) else {}
            except Exception:
                settings_obj = {}
            clients = settings_obj.get('clients') or []
            if not isinstance(clients, list):
                continue
            for c in clients:
                if c.get('email') == username:
                    total_bytes = int(c.get('totalGB', 0) or 0)
                    expiry_ms = int(c.get('expiryTime', 0) or 0)
                    expire = int(expiry_ms / 1000) if expiry_ms > 0 else 0
                    subid = c.get('subId') or ''
                    if self.sub_base:
                        origin = self.sub_base
                    else:
                        parts = urlsplit(self.base_url)
                        host = parts.hostname or ''
                        port = ''
                        if parts.port and not ((parts.scheme == 'http' and parts.port == 80) or (parts.scheme == 'https' and parts.port == 443)):
                            port = f":{parts.port}"
                        origin = f"{parts.scheme}://{host}{port}"
                    sub_link = f"{origin}/sub/{subid}" if subid else ''
                    return {
                        'data_limit': total_bytes,
                        'used_traffic': 0,
                        'expire': expire,
                        'subscription_url': sub_link,
                    }, "Success"
        return None, "کاربر یافت نشد"

    def _fetch_inbound_detail(self, inbound_id: int):
        paths = [
            f"/tx/api/inbounds/get/{inbound_id}",
            f"/xui/api/inbounds/get/{inbound_id}",
            f"/panel/api/inbounds/get/{inbound_id}",
        ]
        for p in paths:
            try:
                resp = self.session.get(f"{self.base_url}{p}", headers={'Accept': 'application/json'}, timeout=12)
                if resp.status_code != 200:
                    continue
                data = resp.json()
                inbound = data.get('obj') if isinstance(data, dict) else data
                if isinstance(inbound, dict):
                    return inbound
            except Exception:
                continue
        return None

    def get_configs_for_user_on_inbound(self, inbound_id: int, username: str, preferred_id: str = None) -> list:
        inbound = self._fetch_inbound_detail(inbound_id)
        if not inbound:
            return []
        # helper to find client by preferred id or email
        def _find_client(inv):
            s = inv.get('settings')
            try:
                obj = json.loads(s) if isinstance(s, str) else (s or {})
            except Exception:
                obj = {}
            chosen = None
            for c in (obj.get('clients') or []):
                if preferred_id and (c.get('id') == preferred_id or c.get('uuid') == preferred_id):
                    return c
                if c.get('email') == username and chosen is None:
                    chosen = c
            return chosen
        client = _find_client(inbound)
        retries = 2
        while client is None and retries > 0:
            _time.sleep(0.7)
            inbound = self._fetch_inbound_detail(inbound_id)
            if not inbound:
                break
            client = _find_client(inbound)
            retries -= 1
        if not client:
            return []
        try:
            settings_str = inbound.get('settings')
            settings_obj = json.loads(settings_str) if isinstance(settings_str, str) else (settings_str or {})
            proto = (inbound.get('protocol') or '').lower()
            port = inbound.get('port') or inbound.get('listen_port') or 0
            stream_raw = inbound.get('streamSettings') or inbound.get('stream_settings')
            stream = json.loads(stream_raw) if isinstance(stream_raw, str) else (stream_raw or {})
            network = (stream.get('network') or '').lower() or 'tcp'
            security = (stream.get('security') or '').lower() or ''
            sni = ''
            if security == 'tls':
                tls = stream.get('tlsSettings') or {}
                sni = tls.get('serverName') or ''
            elif security == 'reality':
                reality = stream.get('realitySettings') or {}
                sni = (reality.get('serverNames') or [''])[0]
            path = ''
            host_header = ''
            service_name = ''
            header_type = ''
            if network == 'ws':
                ws = stream.get('wsSettings') or {}
                path = ws.get('path') or '/'
                headers = ws.get('headers') or {}
                host_header = headers.get('Host') or headers.get('host') or ''
            elif network == 'tcp':
                tcp = stream.get('tcpSettings') or {}
                header = tcp.get('header') or {}
                if (header.get('type') or '').lower() == 'http':
                    header_type = 'http'
                    req = header.get('request') or {}
                    rp = req.get('path')
                    if isinstance(rp, list) and rp:
                        path = rp[0] or '/'
                    elif isinstance(rp, str) and rp:
                        path = rp
                    else:
                        path = '/'
                    h = req.get('headers') or {}
                    hh = h.get('Host') or h.get('host') or ''
                    if isinstance(hh, list) and hh:
                        host_header = hh[0]
                    elif isinstance(hh, str):
                        host_header = hh
            if network == 'grpc':
                grpc = stream.get('grpcSettings') or {}
                service_name = grpc.get('serviceName') or ''
            from urllib.parse import urlsplit as _us
            parts = _us(getattr(self, 'sub_base', '') or self.base_url)
            host = parts.hostname or ''
            if not host:
                host = host_header or sni or host
            uuid_val = client.get('id') or client.get('uuid') or ''
            passwd = client.get('password') or ''
            name = username
            configs = []
            if proto == 'vless' and uuid_val:
                qs = []
                if network:
                    qs.append(f'type={network}')
                if network == 'ws':
                    if path:
                        qs.append(f'path={path}')
                    if host_header:
                        qs.append(f'host={host_header}')
                if network == 'tcp' and header_type == 'http':
                    qs.append('headerType=http')
                    if path:
                        qs.append(f'path={path}')
                    if host_header:
                        qs.append(f'host={host_header}')
                if network == 'grpc' and service_name:
                    qs.append(f'serviceName={service_name}')
                if security:
                    qs.append(f'security={security}')
                    if sni:
                        qs.append(f'sni={sni}')
                else:
                    qs.append('security=none')
                flow = client.get('flow')
                if flow:
                    qs.append(f'flow={flow}')
                query = '&'.join(qs)
                uri = f"vless://{uuid_val}@{host}:{port}?{query}#{name}"
                configs.append(uri)
            elif proto == 'vmess' and uuid_val:
                vm = {
                    "v": "2",
                    "ps": name,
                    "add": host,
                    "port": str(port),
                    "id": uuid_val,
                    "aid": "0",
                    "net": network,
                    "type": "none",
                    "host": host_header or sni or host,
                    "path": path or "/",
                    "tls": "tls" if security in ("tls","reality") else "",
                    "sni": sni or ""
                }
                import base64 as _b64
                b = _b64.b64encode(json.dumps(vm, ensure_ascii=False).encode('utf-8')).decode('utf-8')
                configs.append(f"vmess://{b}")
            elif proto == 'trojan' and passwd:
                qs = []
                if network:
                    qs.append(f'type={network}')
                if network == 'ws':
                    if path:
                        qs.append(f'path={path}')
                    if host_header:
                        qs.append(f'host={host_header}')
                if network == 'grpc' and service_name:
                    qs.append(f'serviceName={service_name}')
                if security:
                    qs.append(f'security={security}')
                    if sni:
                        qs.append(f'sni={sni}')
                query = '&'.join(qs)
                uri = f"trojan://{passwd}@{host}:{port}?{query}#{name}"
                configs.append(uri)
            return configs
        except Exception:
            return []

    async def renew_user_in_panel(self, username, plan):
        if not self.get_token():
            return None, "خطا در ورود به پنل TX-UI"
        inbounds, msg = self.list_inbounds()
        if not inbounds:
            return None, msg
        now_ms = int(datetime.now().timestamp() * 1000)
        try:
            add_bytes = int(float(plan['traffic_gb']) * (1024 ** 3))
        except Exception:
            add_bytes = 0
        try:
            days = int(plan['duration_days'])
            add_ms = days * 86400 * 1000 if days > 0 else 0
        except Exception:
            add_ms = 0
        for ib in inbounds:
            inbound_id = ib.get('id')
            inbound = self._fetch_inbound_detail(inbound_id)
            if not inbound:
                continue
            settings_str = inbound.get('settings')
            clients = []
            try:
                if isinstance(settings_str, str):
                    settings_obj = json.loads(settings_str)
                    clients = settings_obj.get('clients', [])
            except Exception:
                clients = []
            if not isinstance(clients, list):
                continue
            for c in clients:
                if c.get('email') == username:
                    current_exp = int(c.get('expiryTime', 0) or 0)
                    base = max(current_exp, now_ms)
                    target_exp = base + (add_ms if add_ms > 0 else 0)
                    new_total = int(c.get('totalGB', 0) or 0) + (add_bytes if add_bytes > 0 else 0)
                    updated = dict(c)
                    updated['expiryTime'] = target_exp
                    updated['totalGB'] = new_total
                    settings_payload = json.dumps({"clients": [updated]})
                    payload = {"id": int(inbound_id), "settings": settings_payload}
                    for up in ["/tx/api/inbounds/updateClient", "/xui/api/inbounds/updateClient", "/panel/api/inbounds/updateClient"]:
                        try:
                            resp = self.session.post(f"{self.base_url}{up}", headers={'Content-Type': 'application/json'}, json=payload, timeout=15)
                            if resp.status_code in (200, 201):
                                return updated, "Success"
                        except requests.RequestException:
                            continue
                    return None, "به‌روزرسانی کلاینت ناموفق بود"
        return None, "کلاینت برای تمدید یافت نشد"

    def renew_user_on_inbound(self, inbound_id: int, username: str, add_gb: float, add_days: int):
        if not self.get_token():
            return None, "خطا در ورود به پنل TX-UI"
        inbound = self._fetch_inbound_detail(inbound_id)
        if not inbound:
            return None, "اینباند یافت نشد"
        try:
            now_ms = int(datetime.now().timestamp() * 1000)
            settings_str = inbound.get('settings')
            try:
                settings_obj = json.loads(settings_str) if isinstance(settings_str, str) else (settings_str or {})
            except Exception:
                settings_obj = {}
            clients = settings_obj.get('clients') or []
            if not isinstance(clients, list):
                return None, "ساختار کلاینت‌ها نامعتبر است"
            updated = None
            cur_total = 0
            cur_exp_raw = 0
            uuid_old = ''
            for c in clients:
                if c.get('email') == username:
                    current_exp = int(c.get('expiryTime', 0) or 0)
                    cur_exp_raw = current_exp
                    # TX-UI stores ms; detect if seconds
                    is_ms = current_exp > 10**11
                    now_unit = now_ms if is_ms else int(now_ms / 1000)
                    add_unit = (int(add_days) * 86400 * (1000 if is_ms else 1)) if add_days and int(add_days) > 0 else 0
                    base = max(current_exp, now_unit)
                    target_exp = base + add_unit if add_unit > 0 else current_exp
                    add_bytes = int(float(add_gb) * (1024 ** 3)) if add_gb and add_gb > 0 else 0
                    cur_total = int(c.get('totalGB', 0) or 0)
                    new_total = cur_total + (add_bytes if add_bytes > 0 else 0)
                    updated = dict(c)
                    updated['expiryTime'] = target_exp
                    updated['totalGB'] = new_total
                    uuid_old = c.get('id') or c.get('uuid') or ''
                    break
            if not updated:
                return None, "کلاینت یافت نشد"
            # Try update endpoints, including /updateClient/{uuid}
            base_eps = [
                "/tx/api/inbounds/updateClient",
                "/xui/api/inbounds/updateClient",
                "/panel/api/inbounds/updateClient",
            ]
            endpoints = ([f"{e}/{uuid_old}" for e in base_eps] + base_eps) if uuid_old else base_eps
            json_headers = {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}
            form_headers = {'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest'}
            settings_payload = json.dumps({"clients": [updated]})
            payload_json = {"id": int(inbound_id), "settings": settings_payload}
            payload_form = {"id": str(int(inbound_id)), "settings": settings_payload}
            last_err = None
            for ep in endpoints:
                try:
                    r = self.session.post(f"{self.base_url}{ep}", headers=form_headers, data=payload_form, timeout=15)
                    if r.status_code in (200, 201):
                        ref = self._fetch_inbound_detail(inbound_id)
                        try:
                            robj = json.loads(ref.get('settings')) if isinstance(ref.get('settings'), str) else (ref.get('settings') or {})
                        except Exception:
                            robj = {}
                        for c2 in (robj.get('clients') or []):
                            if c2.get('email') == username and int(c2.get('expiryTime', 0) or 0) == updated['expiryTime'] and int(c2.get('totalGB', 0) or 0) == updated['totalGB']:
                                return updated, "Success"
                    r = self.session.post(f"{self.base_url}{ep}", headers=json_headers, json=payload_json, timeout=15)
                    if r.status_code in (200, 201):
                        ref = self._fetch_inbound_detail(inbound_id)
                        try:
                            robj = json.loads(ref.get('settings')) if isinstance(ref.get('settings'), str) else (ref.get('settings') or {})
                        except Exception:
                            robj = {}
                        for c2 in (robj.get('clients') or []):
                            if c2.get('email') == username and int(c2.get('expiryTime', 0) or 0) == updated['expiryTime'] and int(c2.get('totalGB', 0) or 0) == updated['totalGB']:
                                return updated, "Success"
                    # Also try clients array
                    r = self.session.post(f"{self.base_url}{ep}", headers=json_headers, json={"id": int(inbound_id), "clients": [updated]}, timeout=15)
                    if r.status_code in (200, 201):
                        ref = self._fetch_inbound_detail(inbound_id)
                        try:
                            robj = json.loads(ref.get('settings')) if isinstance(ref.get('settings'), str) else (ref.get('settings') or {})
                        except Exception:
                            robj = {}
                        for c2 in (robj.get('clients') or []):
                            if c2.get('email') == username and int(c2.get('expiryTime', 0) or 0) == updated['expiryTime'] and int(c2.get('totalGB', 0) or 0) == updated['totalGB']:
                                return updated, "Success"
                    last_err = f"HTTP {r.status_code}: {(r.text or '')[:160]}"
                except requests.RequestException as e:
                    last_err = str(e)
                    continue
            # Fallback: full inbound update (embed updated client)
            full = self._fetch_inbound_detail(inbound_id) or {}
            try:
                cur_settings = json.loads(full.get('settings')) if isinstance(full.get('settings'), str) else (full.get('settings') or {})
            except Exception:
                cur_settings = {}
            cur_clients = list(cur_settings.get('clients') or [])
            for i, cc in enumerate(cur_clients):
                if cc.get('email') == username:
                    cur_clients[i] = updated
                    break
            else:
                cur_clients.append(updated)
            cur_settings['clients'] = cur_clients
            settings_payload_str = json.dumps(cur_settings)
            full_payload = {
                "id": int(inbound_id),
                "up": full.get('up', 0),
                "down": full.get('down', 0),
                "total": full.get('total', 0),
                "remark": full.get('remark') or "",
                "enable": bool(full.get('enable', True)),
                "expiryTime": full.get('expiryTime', 0) or 0,
                "listen": full.get('listen') or "",
                "port": full.get('port') or 0,
                "protocol": full.get('protocol') or full.get('type') or "vless",
                "settings": settings_payload_str,
                "streamSettings": full.get('streamSettings') or full.get('stream_settings') or "{}",
                "sniffing": full.get('sniffing') or "{}",
                "allocate": full.get('allocate') or "{}",
            }
            up_paths = [
                f"/tx/api/inbounds/update/{int(inbound_id)}",
                f"/xui/api/inbounds/update/{int(inbound_id)}",
                f"/panel/api/inbounds/update/{int(inbound_id)}",
            ]
            for p in up_paths:
                try:
                    rr = self.session.post(f"{self.base_url}{p}", headers=json_headers, json=full_payload, timeout=15)
                    if rr.status_code in (200, 201):
                        ref2 = self._fetch_inbound_detail(inbound_id)
                        try:
                            robj2 = json.loads(ref2.get('settings')) if isinstance(ref2.get('settings'), str) else (ref2.get('settings') or {})
                        except Exception:
                            robj2 = {}
                        for c2 in (robj2.get('clients') or []):
                            if c2.get('email') == username and int(c2.get('expiryTime', 0) or 0) == updated['expiryTime'] and int(c2.get('totalGB', 0) or 0) == updated['totalGB']:
                                return updated, "Success"
                except requests.RequestException:
                    continue
            return None, (last_err or "به‌روزرسانی کلاینت ناموفق بود")
        except Exception as e:
            return None, str(e)


class MarzneshinAPI(BasePanelAPI):
    """Marzneshin support via /api endpoints with Bearer token.
    - Requires admin API token (Authorization: Bearer <TOKEN>)
    - Endpoints used:
        /api/users, /api/inbounds, /api/configs
    - Fallback X-UI cookie login is disabled to avoid hitting /login on API-only deployments.
    """

    def __init__(self, panel_row):
        self.panel_id = panel_row['id']
        self.base_url = panel_row['url'].rstrip('/')
        # Some deployments may serve under /app, but official docs use root /api
        self.api_base = self.base_url
        self.username = panel_row.get('username')
        self.password = panel_row.get('password')
        self.token = (panel_row.get('token') or '').strip()
        self.sub_base = (panel_row.get('sub_base') or '').strip().rstrip('/') if isinstance(panel_row, dict) else ''
        self.session = requests.Session()
        self._json_headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        self._last_token_error = None
        
    def _log_json(self, title: str, data):
        try:
            import json as _json
            text = _json.dumps(data, ensure_ascii=False)
        except Exception:
            text = str(data)
        try:
            from .config import logger as _lg
            _lg.info(f"[Marzneshin] {title}: {text[:4000]}")
        except Exception:
            pass

    def _token_header_variants(self):
        if not self.token:
            return []
        return [
            {'Accept': 'application/json', 'Authorization': f"Bearer {self.token}"},
        ]

    def _extract_token_from_obj(self, obj):
        if isinstance(obj, dict):
            # direct keys first
            for k in ['access_token', 'token', 'bearer', 'Authorization']:
                if k in obj and isinstance(obj[k], str) and len(obj[k]) >= 8:
                    return obj[k]
            # nested search
            for v in obj.values():
                t = self._extract_token_from_obj(v)
                if t:
                    return t
        elif isinstance(obj, list):
            for v in obj:
                t = self._extract_token_from_obj(v)
                if t:
                    return t
        elif isinstance(obj, str) and len(obj) >= 8:
            return obj
        return None

    def _ensure_token(self) -> bool:
        if self.token:
            return True
        # Try to obtain token using username/password via common API login endpoints
        if not (self.username and self.password):
            return False
        # Build base candidates: provided URL and origin (scheme://host:port)
        bases = []
        bu = self.base_url.rstrip('/')
        bases.append(bu)
        try:
            parts = urlsplit(self.base_url)
            host = parts.hostname or ''
            if host:
                port = ''
                if parts.port and not ((parts.scheme == 'http' and parts.port == 80) or (parts.scheme == 'https' and parts.port == 443)):
                    port = f":{parts.port}"
                origin = f"{parts.scheme}://{host}{port}"
                if origin not in bases:
                    bases.append(origin)
        except Exception:
            pass
        # Also add variant with '/app' stripped if present
        if bu.endswith('/app'):
            root = bu[:-4]
            if root and root not in bases:
                bases.append(root)
        else:
            # Also try with '/app' appended
            app_base = f"{bu}/app"
            if app_base not in bases:
                bases.append(app_base)
            try:
                # origin + /app
                if 'origin' in locals():
                    app_origin = f"{origin}/app"
                    if app_origin not in bases:
                        bases.append(app_origin)
            except Exception:
                pass

        # Try multiple token endpoints (form and JSON) commonly used by Marzneshin
        last_err = None
        for base in bases:
            candidates = [
                (f"{base}/api/admins/token", "form"),
                (f"{base}/api/admins/token/", "form"),
                (f"{base}/api/auth/token", "form"),
                (f"{base}/api/auth/login", "json"),
            ]
            for url, mode in candidates:
                try:
                    if mode == "form":
                        resp = self.session.post(url, data={"username": self.username, "password": self.password, "grant_type": "password"}, headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}, timeout=12)
                    else:
                        resp = self.session.post(url, json={"username": self.username, "password": self.password}, headers={"Accept": "application/json", "Content-Type": "application/json"}, timeout=12)
                    if resp.status_code not in (200, 201):
                        last_err = f"HTTP {resp.status_code} @ {url}"
                        continue
                    try:
                        data = resp.json()
                    except ValueError:
                        last_err = f"non-JSON @ {url}"
                        continue
                    token_val = self._extract_token_from_obj(data)
                    if isinstance(token_val, str) and token_val:
                        if token_val.lower().startswith("bearer "):
                            token_val = token_val[7:].strip()
                        self.token = token_val.strip()
                        self._last_token_error = None
                        return True
                    last_err = f"no token in response @ {url}"
                except requests.RequestException:
                    last_err = f"request error @ {url}"
                    continue
        if last_err:
            self._last_token_error = last_err
            from .config import logger
            logger.error(f"Marzneshin: failed to obtain token: {last_err}")
        return False

    def _find_first_list_of_dicts(self, obj):
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            return obj
        if isinstance(obj, dict):
            for v in obj.values():
                res = self._find_first_list_of_dicts(v)
                if isinstance(res, list):
                    return res
        if isinstance(obj, list):
            for v in obj:
                res = self._find_first_list_of_dicts(v)
                if isinstance(res, list):
                    return res
        return None

    def list_inbounds(self):
        try:
            # Token-based API attempts (required for Marzneshin)
            if not self.token and not self._ensure_token():
                detail = (self._last_token_error or "نامشخص")
                return None, f"توکن دریافت نشد: {detail}"
            if self.token:
                # Per docs: use only /api/inbounds (optionally with page/size)
                bases = []
                bu = self.base_url.rstrip('/')
                bases.append(bu)
                try:
                    parts = urlsplit(self.base_url)
                    host = parts.hostname or ''
                    if host:
                        port = ''
                        if parts.port and not ((parts.scheme == 'http' and parts.port == 80) or (parts.scheme == 'https' and parts.port == 443)):
                            port = f":{parts.port}"
                        origin = f"{parts.scheme}://{host}{port}"
                        if origin not in bases:
                            bases.append(origin)
                except Exception:
                    pass
                endpoints = [f"{b}/api/inbounds" for b in bases]
                last_err = None
                tried_refresh = False
                header_sets = self._token_header_variants()
                for url in endpoints:
                    for candidate in [url, f"{url}?page=1&size=100"]:
                        for hdrs in header_sets:
                            try:
                                resp = self.session.get(candidate, headers=hdrs, timeout=12)
                            except requests.RequestException as e:
                                last_err = str(e)
                                continue
                            if resp.status_code == 401 and not tried_refresh:
                                # try to refresh token once
                                if self._ensure_token():
                                    tried_refresh = True
                                    header_sets = self._token_header_variants()
                                    continue
                            if resp.status_code != 200:
                                last_err = f"HTTP {resp.status_code} @ {candidate}"
                                continue
                            try:
                                data = resp.json()
                            except ValueError:
                                last_err = f"non-JSON @ {candidate}"
                                continue
                            items = self._find_first_list_of_dicts(data)
                            if not isinstance(items, list):
                                last_err = "لیست اینباند نامعتبر است"
                                continue
                            inbounds = []
                            for it in items:
                                if not isinstance(it, dict):
                                    continue
                                inbounds.append({
                                    'id': it.get('id'),
                                    'remark': it.get('remark') or it.get('tag') or str(it.get('id')),
                                    'protocol': it.get('protocol') or it.get('type') or 'unknown',
                                    'port': it.get('port') or it.get('listen_port') or 0,
                                })
                            return inbounds, "Success"
                if last_err:
                    logger.error(f"Marzneshin list_inbounds (token) error: {last_err}")
            # No token provided -> do not attempt cookie login for Marزنسhin
            detail = (self._last_token_error or "نامشخص")
            return None, f"توکن دریافت نشد: {detail}"
        except requests.RequestException as e:
            logger.error(f"Marzneshin list_inbounds error: {e}")
            return None, str(e)

    def create_user_on_inbound(self, inbound_id: int, user_id: int, plan):
        try:
            import random, string
            subid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=12))
            try:
                traffic_gb = float(plan['traffic_gb'])
            except Exception:
                traffic_gb = 0.0
            total_bytes = int(traffic_gb * (1024 ** 3)) if traffic_gb > 0 else 0
            client_obj = {
                "id": str(uuid.uuid4()),
                "email": f"user_{subid}",
                "totalGB": total_bytes,
                "expiryTime": 0,
                "enable": True,
                "limitIp": 0,
                "subId": subid,
                "reset": 0
            }
            settings_obj = {"clients": [client_obj]}
            # Token-based attempts (Marzneshin official API does not add client per inbound; keep for compatibility if needed)
            if not self.token and not self._ensure_token():
                detail = (self._last_token_error or "نامشخص")
                return None, None, f"توکن دریافت نشد: {detail}"
            if self.token:
                # Prefer official user creation via /api/users
                last_err = None
                for hdrs in self._token_header_variants():
                    try:
                        # Create user first
                        payload_user = {
                            "username": f"user_{user_id}_{uuid.uuid4().hex[:6]}",
                        }
                        # Map plan to expire (days) and data_limit (e.g., 10GB/200MB)
                        try:
                            days = int(plan['duration_days'])
                        except Exception:
                            days = 0
                        if days > 0:
                            payload_user["expire"] = days
                        try:
                            tgb = float(plan['traffic_gb'])
                        except Exception:
                            tgb = 0.0
                        if tgb > 0:
                            if tgb >= 1 and abs(tgb - round(tgb)) < 1e-6:
                                payload_user["data_limit"] = f"{int(round(tgb))}GB"
                            elif tgb >= 1:
                                payload_user["data_limit"] = f"{tgb}GB"
                            else:
                                payload_user["data_limit"] = f"{int(round(tgb * 1024))}MB"
                        resp_user = self.session.post(f"{self.base_url}/api/users", headers=hdrs, json=payload_user, timeout=15)
                        if resp_user.status_code not in (200, 201):
                            last_err = f"HTTP {resp_user.status_code} @ /api/users: {(resp_user.text or '')[:200]}"
                            continue
                        try:
                            juser = resp_user.json()
                        except ValueError:
                            juser = {}
                        created_username = juser.get('username') or payload_user["username"]
                        # Try to fetch configs for this user to build link(s)
                        sub_link = ''
                        for cfg_url in [
                            f"{self.base_url}/api/configs?username={created_username}",
                            f"{self.base_url}/api/configs",
                        ]:
                            try:
                                rc = self.session.get(cfg_url, headers=hdrs, timeout=12)
                                if rc.status_code != 200:
                                    continue
                                data = rc.json()
                            except Exception:
                                continue
                            items = data if isinstance(data, list) else (data.get('configs') if isinstance(data, dict) else [])
                            if not isinstance(items, list):
                                continue
                            links = []
                            for it in items:
                                if not isinstance(it, dict):
                                    continue
                                owner = it.get('username') or it.get('user') or it.get('email')
                                if owner and owner != created_username and 'username' in cfg_url:
                                    # when filtered by username, accept all
                                    pass
                                elif owner and owner != created_username:
                                    continue
                                link = it.get('link') or it.get('url') or it.get('config')
                                if isinstance(link, str) and link.strip():
                                    links.append(link.strip())
                            if links:
                                sub_link = "\n".join(links)
                                break
                        return created_username, sub_link or None, "Success"
                    except requests.RequestException as e:
                        last_err = str(e)
                        continue
                return None, None, last_err or "API failure"
            # No token -> do not attempt cookie login for Marzneshin
            return None, None, "برای مرزنشین باید Token API تنظیم شود (apiv2)."
        except requests.RequestException as e:
            logger.error(f"Marzneshin create_user_on_inbound error: {e}")
            return None, None, str(e)

    async def get_user(self, username):
        # Marzneshin: use /api/users/{username} for core info and /sub/{username}/{key}/info|usage for stats
        # 1) Ensure token and get user
        if not self.token and not self._ensure_token():
            detail = (self._last_token_error or "نامشخص")
            return None, f"توکن دریافت نشد: {detail}"
        try:
            ru = self.session.get(f"{self.base_url}/api/users/{username}", headers={"Accept": "application/json", "Authorization": f"Bearer {self.token}"}, timeout=12)
            if ru.status_code == 404:
                return None, "کاربر یافت نشد"
            if ru.status_code != 200:
                return None, f"HTTP {ru.status_code} @ /api/users/{username}"
            u = ru.json() if ru.headers.get('content-type','').lower().startswith('application/json') else {}
        except requests.RequestException as e:
            return None, str(e)

        # Extract data_limit, expire
        data_limit = 0
        expire_ts = 0
        try:
            dl = u.get('data_limit')
            if isinstance(dl, (int, float)):
                data_limit = int(dl)
        except Exception:
            pass
        try:
            # prefer epoch seconds if provided
            if isinstance(u.get('expire'), (int, float)):
                expire_ts = int(u['expire'])
            else:
                # ISO string in expire_date
                ed = u.get('expire_date') or u.get('expireDate')
                if isinstance(ed, str) and ed:
                    from datetime import datetime
                    try:
                        expire_ts = int(datetime.fromisoformat(ed.replace('Z', '+00:00')).timestamp())
                    except Exception:
                        expire_ts = 0
        except Exception:
            pass

        # 2) Get subscription_url to derive sub key
        sub_url = u.get('subscription_url') or u.get('subscription') or ''
        if isinstance(sub_url, str) and sub_url and not sub_url.startswith('http'):
            sub_url = f"{self.base_url}{sub_url}"

        # Parse username/key from subscription url if possible
        sub_user = None
        sub_key = None
        if isinstance(sub_url, str) and sub_url:
            try:
                import re as _re
                m = _re.search(r"/sub/([^/]+)/([^/?#]+)", sub_url)
                if m:
                    sub_user, sub_key = m.group(1), m.group(2)
            except Exception:
                pass

        used_traffic = 0
        # 3) Query public sub info/usage endpoints if key available
        if sub_user and sub_key:
            origin = f"{urlsplit(self.base_url).scheme}://{urlsplit(self.base_url).hostname}{(':'+str(urlsplit(self.base_url).port)) if urlsplit(self.base_url).port and not ((urlsplit(self.base_url).scheme=='http' and urlsplit(self.base_url).port==80) or (urlsplit(self.base_url).scheme=='https' and urlsplit(self.base_url).port==443)) else ''}"
            info_url = f"{origin}/sub/{sub_user}/{sub_key}/info"
            usage_url = f"{origin}/sub/{sub_user}/{sub_key}/usage"
            try:
                ri = self.session.get(info_url, headers={"Accept": "application/json"}, timeout=10)
                if ri.status_code == 200:
                    try:
                        info = ri.json()
                        # attempt to override data_limit/expire from info if present
                        if isinstance(info, dict):
                            if isinstance(info.get('data_limit'), (int, float)):
                                data_limit = int(info['data_limit'])
                            if isinstance(info.get('expire'), (int, float)):
                                expire_ts = int(info['expire'])
                    except Exception:
                        pass
            except requests.RequestException:
                pass
            try:
                ru2 = self.session.get(usage_url, headers={"Accept": "application/json"}, timeout=10)
                if ru2.status_code == 200:
                    try:
                        usage = ru2.json()
                        if isinstance(usage, dict):
                            # common keys: used, download+upload, total
                            if isinstance(usage.get('used'), (int, float)):
                                used_traffic = int(usage['used'])
                            else:
                                down = usage.get('download') or usage.get('down') or 0
                                up = usage.get('upload') or usage.get('up') or 0
                                if isinstance(down, (int, float)) or isinstance(up, (int, float)):
                                    used_traffic = int(down or 0) + int(up or 0)
                    except Exception:
                        pass
            except requests.RequestException:
                pass

        return {
            'data_limit': data_limit,
            'used_traffic': used_traffic,
            'expire': expire_ts,
            'subscription_url': sub_url or '',
        }, "Success"

    async def renew_user_in_panel(self, username, plan):
        # Marzneshin renewal via PUT /api/users/{username}: add days and bytes
        if not self.token and not self._ensure_token():
            detail = (self._last_token_error or "نامشخص")
            return None, f"توکن دریافت نشد: {detail}"
        # Fetch current user
        try:
            ru = self.session.get(f"{self.base_url}/api/users/{username}", headers={"Accept": "application/json", "Authorization": f"Bearer {self.token}"}, timeout=12)
            if ru.status_code != 200:
                return None, f"HTTP {ru.status_code} @ /api/users/{username}"
            u = ru.json() if ru.headers.get('content-type','').lower().startswith('application/json') else {}
        except requests.RequestException as e:
            return None, str(e)

        # Compute increments
        try:
            add_bytes = int(float(plan['traffic_gb']) * (1024 ** 3))
        except Exception:
            add_bytes = 0
        try:
            add_days = int(plan['duration_days'])
        except Exception:
            add_days = 0

        # Current values
        current_dl = u.get('data_limit') if isinstance(u, dict) else None
        target_dl = None
        try:
            cur = int(current_dl) if current_dl is not None else None
            if add_bytes > 0:
                target_dl = (cur or 0) + add_bytes
        except Exception:
            target_dl = None

        from datetime import datetime, timedelta
        target_expire_date = None
        try:
            ed = u.get('expire_date') or u.get('expireDate')
            if isinstance(ed, str) and add_days > 0:
                base_dt = datetime.fromisoformat(ed.replace('Z', '+00:00'))
                target_expire_date = (base_dt + timedelta(days=add_days)).isoformat()
            elif add_days > 0:
                target_expire_date = (datetime.utcnow() + timedelta(days=add_days)).isoformat()
        except Exception:
            if add_days > 0:
                target_expire_date = (datetime.utcnow() + timedelta(days=add_days)).isoformat()

        update_body = {"username": username}
        if target_dl is not None:
            update_body["data_limit"] = int(target_dl)
        else:
            update_body["data_limit"] = None  # no change
        if target_expire_date is not None:
            update_body["expire_date"] = target_expire_date
        else:
            update_body["expire_date"] = None

        try:
            rp = self.session.put(f"{self.base_url}/api/users/{username}", headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}, json=update_body, timeout=15)
            if rp.status_code not in (200, 201):
                return None, f"HTTP {rp.status_code} @ /api/users/{username}: {(rp.text or '')[:200]}"
            return rp.json() if rp.headers.get('content-type','').lower().startswith('application/json') else update_body, "Success"
        except requests.RequestException as e:
            return None, str(e)

    async def create_user(self, user_id, plan):
        # Ensure token
        if not self.token and not self._ensure_token():
            detail = (self._last_token_error or "نامشخص")
            return None, None, f"توکن دریافت نشد: {detail}"
        # Build payload like sample bot
        try:
            settings = query_db("SELECT protocol, tag FROM panel_inbounds WHERE panel_id = ?", (self.panel_id,)) or []
        except Exception:
            settings = []
        # Collect service_ids: prefer live from API (/api/services). If none, create a service using all inbound IDs
        service_ids: list[int] = []
        # 1) Fetch existing services
        try:
            for url in [f"{self.base_url}/api/services", f"{self.base_url}/api/services?page=1&size=100"]:
                r = self.session.get(url, headers={"Accept": "application/json", "Authorization": f"Bearer {self.token}"}, timeout=12)
                if r.status_code != 200:
                    continue
                data = r.json()
                self._log_json(f"GET {url}", data)
                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get('services') or data.get('items') or data.get('obj') or []
                if isinstance(items, list):
                    for it in items:
                        if isinstance(it, dict) and isinstance(it.get('id'), int):
                            service_ids.append(it['id'])
                    if service_ids:
                        break
        except Exception:
            pass
        # 2) If no service exists, create one with all inbounds
        if not service_ids:
            try:
                # get inbound ids
                inbound_ids: list[int] = []
                for url in [f"{self.base_url}/api/inbounds", f"{self.base_url}/api/inbounds?page=1&size=100"]:
                    ri = self.session.get(url, headers={"Accept": "application/json", "Authorization": f"Bearer {self.token}"}, timeout=12)
                    if ri.status_code != 200:
                        continue
                    di = ri.json()
                    self._log_json(f"GET {url}", di)
                    arr = di if isinstance(di, list) else (di.get('inbounds') if isinstance(di, dict) else [])
                    if isinstance(arr, list):
                        for it in arr:
                            if isinstance(it, dict) and isinstance(it.get('id'), int):
                                inbound_ids.append(it['id'])
                        if inbound_ids:
                            break
                if inbound_ids:
                    name = f"auto_service_{uuid.uuid4().hex[:6]}"
                    payload_service = {"inbound_ids": inbound_ids, "name": name}
                    rs = self.session.post(f"{self.base_url}/api/services", headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}, json=payload_service, timeout=15)
                    if rs.status_code in (200, 201):
                        try:
                            js = rs.json()
                            self._log_json("POST /api/services response", js)
                            if isinstance(js, dict) and isinstance(js.get('id'), int):
                                service_ids = [js['id']]
                        except Exception:
                            pass
                # Fallback to DB tags as inbound ids -> create service
                if not service_ids and settings:
                    inbound_ids = []
                    for row in settings:
                        tag = row.get('tag')
                        if isinstance(tag, str) and tag.strip():
                            try:
                                inbound_ids.append(int(tag.strip()))
                            except Exception:
                                continue
                    if inbound_ids:
                        name = f"auto_service_{uuid.uuid4().hex[:6]}"
                        payload_service = {"inbound_ids": inbound_ids, "name": name}
                        rs = self.session.post(f"{self.base_url}/api/services", headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}, json=payload_service, timeout=15)
                        if rs.status_code in (200, 201):
                            try:
                                js = rs.json()
                                self._log_json("POST /api/services response (DB fallback)", js)
                                if isinstance(js, dict) and isinstance(js.get('id'), int):
                                    service_ids = [js['id']]
                            except Exception:
                                pass
            except Exception:
                pass
        # Map traffic/days
        try:
            tgb = float(plan['traffic_gb'])
        except Exception:
            tgb = 0.0
        # Marzneshin expects integer for data_limit; use bytes
        data_limit = int(tgb * (1024 ** 3)) if tgb > 0 else None
        try:
            days = int(plan['duration_days'])
        except Exception:
            days = 0
        expire_date = None
        expire_strategy = "never"
        usage_duration = None
        if days > 0:
            # fixed date default
            from datetime import datetime, timedelta
            dt = (datetime.utcnow() + timedelta(days=days)).isoformat()
            expire_date = dt
            expire_strategy = "fixed_date"
        new_username = f"user_{user_id}_{uuid.uuid4().hex[:6]}"
        payload = {
            "username": new_username,
        }
        if service_ids:
            payload["service_ids"] = service_ids
        if data_limit is not None:
            payload["data_limit"] = int(data_limit)
        payload["expire_strategy"] = expire_strategy
        payload["expire_date"] = expire_date
        if usage_duration is not None:
            payload["usage_duration"] = usage_duration

        try:
            resp = self.session.post(f"{self.base_url}/api/users", headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}, json=payload, timeout=15)
            if resp.status_code not in (200, 201):
                return None, None, f"HTTP {resp.status_code} @ /api/users: {(resp.text or '')[:200]}"
            # Ensure services are attached to user by explicit PUT
            if service_ids:
                try:
                    ru = self.session.put(f"{self.base_url}/api/users/{new_username}", headers={"Accept": "application/json", "Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}, json={"service_ids": service_ids}, timeout=12)
                    # ignore status; best-effort
                    _ = ru.status_code
                except Exception:
                    pass
            # Try to fetch user info for subscription URL first
            sub_link = ''
            try:
                r_user = self.session.get(f"{self.base_url}/api/users/{new_username}", headers={"Accept": "application/json", "Authorization": f"Bearer {self.token}"}, timeout=12)
                if r_user.status_code == 200:
                    u = r_user.json() if r_user.headers.get('content-type','').lower().startswith('application/json') else {}
                    if isinstance(u, dict):
                        s = u.get('subscription_url') or u.get('subscription') or ''
                        if isinstance(s, str) and s.strip():
                            if s.startswith('http'):
                                sub_link = s.strip()
                            else:
                                sub_link = f"{self.base_url}{s.strip()}"
                        # Sometimes configs array is present on user
                        if not sub_link and isinstance(u.get('configs'), list):
                            links = []
                            for it in u.get('configs'):
                                if isinstance(it, dict):
                                    link = it.get('link') or it.get('url') or it.get('config')
                                    if isinstance(link, str) and link.strip():
                                        links.append(link.strip())
                            if links:
                                sub_link = "\n".join(links)
            except Exception:
                pass
            # Fallback: fetch configs endpoint filtered by username
            if not sub_link:
                try:
                    r2 = self.session.get(f"{self.base_url}/api/configs?username={new_username}", headers={"Accept": "application/json", "Authorization": f"Bearer {self.token}"}, timeout=12)
                    if r2.status_code == 200:
                        data = r2.json()
                        items = data if isinstance(data, list) else (data.get('configs') if isinstance(data, dict) else [])
                        links = []
                        if isinstance(items, list):
                            for it in items:
                                if isinstance(it, dict):
                                    owner = it.get('username') or it.get('user') or it.get('email')
                                    if owner and owner != new_username:
                                        continue
                                    link = it.get('link') or it.get('url') or it.get('config')
                                    if isinstance(link, str) and link.strip():
                                        links.append(link.strip())
                        if links:
                            sub_link = "\n".join(links)
                except Exception:
                    pass
            return new_username, (sub_link or None), "Success"
        except requests.RequestException as e:
            return None, None, str(e)

    def rotate_user_key(self, username: str) -> bool:
        # Iterate inbounds, find client by email and rotate its credentials
        inbounds, msg = self.list_inbounds()
        if not inbounds:
            return False
        changed = False
        for ib in inbounds:
            inbound_id = ib.get('id')
            inbound = self._fetch_inbound_detail(inbound_id)
            if not inbound:
                continue
            try:
                import json as _json, uuid as _uuid, random as _rand, string as _str
                settings_str = inbound.get('settings')
                settings_obj = _json.loads(settings_str) if isinstance(settings_str, str) else (settings_str or {})
                clients = settings_obj.get('clients') or []
                if not isinstance(clients, list):
                    continue
                proto = (inbound.get('protocol') or inbound.get('type') or '').lower()
                for idx, c in enumerate(clients):
                    if c.get('email') == username:
                        updated = dict(c)
                        old_uuid = c.get('id') or c.get('uuid') or None
                        # Rotate identity based on protocol
                        if proto in ('vless','vmess'):
                            updated['id'] = str(_uuid.uuid4())
                        elif proto == 'trojan':
                            updated['password'] = ''.join(_rand.choices(_str.ascii_letters + _str.digits, k=16))
                        # Always rotate subId when present
                        if 'subId' in updated:
                            updated['subId'] = ''.join(_rand.choices(_str.ascii_lowercase + _str.digits, k=12))
                        # Push update via API
                        settings_payload = _json.dumps({"clients": [updated]})
                        payload = {"id": int(inbound_id), "settings": settings_payload}
                        endpoints = [
                            "/xui/api/inbounds/updateClient",
                            "/panel/api/inbounds/updateClient",
                            "/xui/api/inbound/updateClient",
                        ]
                        if old_uuid:
                            endpoints = [f"{e}/{old_uuid}" for e in endpoints] + endpoints
                        for ep in endpoints:
                            try:
                                resp = self.session.post(f"{self.base_url}{ep}", headers={'Content-Type': 'application/json'}, json=payload, timeout=15)
                                if resp.status_code in (200, 201):
                                    changed = True
                                    break
                            except requests.RequestException:
                                continue
                # continue checking other inbounds
            except Exception:
                continue
        return changed
        try:
            import json as _json
            settings_str = inbound.get('settings')
            settings_obj = _json.loads(settings_str) if isinstance(settings_str, str) else (settings_str or {})
            clients = settings_obj.get('clients') or []
            client = None
            for c in clients:
                if c.get('email') == username:
                    client = c
                    break
            if not client:
                return []
            proto = (inbound.get('protocol') or '').lower()
            port = inbound.get('port') or inbound.get('listen_port') or 0
            # stream settings
            stream_raw = inbound.get('streamSettings') or inbound.get('stream_settings')
            stream = _json.loads(stream_raw) if isinstance(stream_raw, str) else (stream_raw or {})
            network = (stream.get('network') or '').lower() or 'tcp'
            security = (stream.get('security') or '').lower() or ''
            # tls/sni
            sni = ''
            if security == 'tls':
                tls = stream.get('tlsSettings') or {}
                sni = tls.get('serverName') or ''
            elif security == 'reality':
                reality = stream.get('realitySettings') or {}
                sni = (reality.get('serverNames') or [''])[0]
            # ws/grpc
            path = ''
            host_header = ''
            service_name = ''
            header_type = ''
            if network == 'ws':
                ws = stream.get('wsSettings') or {}
                path = ws.get('path') or '/'
                headers = ws.get('headers') or {}
                host_header = headers.get('Host') or headers.get('host') or ''
            elif network == 'tcp':
                tcp = stream.get('tcpSettings') or {}
                header = tcp.get('header') or {}
                if (header.get('type') or '').lower() == 'http':
                    header_type = 'http'
                    req = header.get('request') or {}
                    # path can be list or string
                    rp = req.get('path')
                    if isinstance(rp, list) and rp:
                        path = rp[0] or '/'
                    elif isinstance(rp, str) and rp:
                        path = rp
                    else:
                        path = '/'
                    h = req.get('headers') or {}
                    # Host header may be list
                    hh = h.get('Host') or h.get('host') or ''
                    if isinstance(hh, list) and hh:
                        host_header = hh[0]
                    elif isinstance(hh, str):
                        host_header = hh
            if network == 'grpc':
                grpc = stream.get('grpcSettings') or {}
                service_name = grpc.get('serviceName') or ''
            # host (domain)
            from urllib.parse import urlsplit as _us
            # Prefer sub_base host if present; otherwise derive from base_url
            parts = _us(getattr(self, 'sub_base', '') or self.base_url)
            host = parts.hostname or ''
            if not host:
                # Fallback: if inbound has SNIs/hosts, use them for host
                host = host_header or sni or host
            # client id/password
            uuid = client.get('id') or client.get('uuid') or ''
            passwd = client.get('password') or ''
            name = username
            configs = []
            if proto == 'vless' and uuid:
                qs = []
                if network:
                    qs.append(f'type={network}')
                if network == 'ws':
                    if path:
                        qs.append(f'path={path}')
                    if host_header:
                        qs.append(f'host={host_header}')
                if network == 'tcp' and header_type == 'http':
                    qs.append('headerType=http')
                    if path:
                        qs.append(f'path={path}')
                    if host_header:
                        qs.append(f'host={host_header}')
                if network == 'grpc' and service_name:
                    qs.append(f'serviceName={service_name}')
                if security:
                    qs.append(f'security={security}')
                    if sni:
                        qs.append(f'sni={sni}')
                else:
                    qs.append('security=none')
                flow = client.get('flow')
                if flow:
                    qs.append(f'flow={flow}')
                query = '&'.join(qs)
                uri = f"vless://{uuid}@{host}:{port}?{query}#{name}"
                configs.append(uri)
            elif proto == 'vmess' and uuid:
                vm = {
                    "v": "2",
                    "ps": name,
                    "add": host,
                    "port": str(port),
                    "id": uuid,
                    "aid": "0",
                    "net": network,
                    "type": "none",
                    "host": host_header or sni or host,
                    "path": path or "/",
                    "tls": "tls" if security in ("tls","reality") else "",
                    "sni": sni or ""
                }
                import base64 as _b64
                b = _b64.b64encode(_json.dumps(vm, ensure_ascii=False).encode('utf-8')).decode('utf-8')
                configs.append(f"vmess://{b}")
            elif proto == 'trojan' and passwd:
                qs = []
                if network:
                    qs.append(f'type={network}')
                if network == 'ws':
                    if path:
                        qs.append(f'path={path}')
                    if host_header:
                        qs.append(f'host={host_header}')
                if network == 'grpc' and service_name:
                    qs.append(f'serviceName={service_name}')
                if security:
                    qs.append(f'security={security}')
                    if sni:
                        qs.append(f'sni={sni}')
                else:
                    qs.append('security=none')
                query = '&'.join(qs)
                uri = f"trojan://{passwd}@{host}:{port}?{query}#{name}"
                configs.append(uri)
            return configs
        except Exception:
            return []

    def rotate_user_key_on_inbound(self, inbound_id: int, username: str):
        # Ensure we are logged in before attempting update
        try:
            self.get_token()
        except Exception:
            pass
        inbound = self._fetch_inbound_detail(inbound_id)
        if not inbound:
            return None
        try:
            import json as _json, uuid as _uuid, random as _rand, string as _str
            settings_str = inbound.get('settings')
            settings_obj = _json.loads(settings_str) if isinstance(settings_str, str) else (settings_str or {})
            clients = settings_obj.get('clients') or []
            if not isinstance(clients, list):
                return None
            proto = (inbound.get('protocol') or inbound.get('type') or '').lower()
            updated = None; idx = -1; old_uuid = None
            for i, c in enumerate(clients):
                if c.get('email') == username:
                    updated = dict(c); idx = i
                    old_uuid = c.get('id') or c.get('uuid') or None
                    if proto in ('vless','vmess'):
                        updated['id'] = str(_uuid.uuid4())
                    elif proto == 'trojan':
                        updated['password'] = ''.join(_rand.choices(_str.ascii_letters + _str.digits, k=16))
                    # Rotate subId if present
                    if 'subId' in updated:
                        updated['subId'] = ''.join(_rand.choices(_str.ascii_lowercase + _str.digits, k=12))
                    break
            if not updated:
                return None
            # Replace in full settings and push update via multiple formats
            full_settings = settings_obj
            full_clients = list(clients)
            if idx >= 0:
                full_clients[idx] = updated
                full_settings['clients'] = full_clients
            settings_payload = _json.dumps(full_settings)
            json_headers = {'Accept': 'application/json', 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'}
            form_headers = {'Accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest'}
            base_endpoints = [
                "/xui/API/inbounds/updateClient",
                "/panel/API/inbounds/updateClient",
                "/xui/api/inbounds/updateClient",
                "/panel/api/inbounds/updateClient",
                "/xui/api/inbound/updateClient",
            ]
            endpoints = []
            if old_uuid:
                for be in base_endpoints:
                    if be.endswith('updateClient'):
                        endpoints.append(f"{be}/{old_uuid}")
            endpoints.extend(base_endpoints)
            # Try multiple formats per endpoint
            last_preview = None
            for ep in endpoints:
                # A) JSON with settings string
                try:
                    payload_a = {"id": int(inbound_id), "settings": settings_payload}
                    resp = self.session.post(f"{self.base_url}{ep}", headers=json_headers, json=payload_a, timeout=15)
                    if resp.status_code in (200, 201):
                        _new = self._fetch_inbound_detail(inbound_id)
                        try:
                            _s = _json.loads(_new.get('settings')) if isinstance(_new.get('settings'), str) else (_new.get('settings') or {})
                        except Exception:
                            _s = {}
                        for c2 in (_s.get('clients') or []):
                            if c2.get('email') == username:
                                if proto in ('vless','vmess'):
                                    if c2.get('id') == updated.get('id'):
                                        return updated
                                elif proto == 'trojan':
                                    if c2.get('password') == updated.get('password'):
                                        return updated
                    else:
                        last_preview = f"{ep} -> HTTP {resp.status_code}: {(resp.text or '')[:180]}"
                except requests.RequestException as _e:
                    last_preview = f"{ep} -> EXC {_e}"
                # B) form-urlencoded with settings
                try:
                    payload_b = {"id": str(int(inbound_id)), "settings": settings_payload}
                    resp = self.session.post(f"{self.base_url}{ep}", headers=form_headers, data=payload_b, timeout=15)
                    if resp.status_code in (200, 201):
                        _new = self._fetch_inbound_detail(inbound_id)
                        try:
                            _s = _json.loads(_new.get('settings')) if isinstance(_new.get('settings'), str) else (_new.get('settings') or {})
                        except Exception:
                            _s = {}
                        for c2 in (_s.get('clients') or []):
                            if c2.get('email') == username:
                                if proto in ('vless','vmess'):
                                    if c2.get('id') == updated.get('id'):
                                        return updated
                                elif proto == 'trojan':
                                    if c2.get('password') == updated.get('password'):
                                        return updated
                    else:
                        last_preview = f"{ep} -> HTTP {resp.status_code}: {(resp.text or '')[:180]}"
                except requests.RequestException:
                    last_preview = f"{ep} -> EXC form"
                # C) JSON with clients array
                try:
                    payload_c = {"id": int(inbound_id), "clients": full_clients}
                    resp = self.session.post(f"{self.base_url}{ep}", headers=json_headers, json=payload_c, timeout=15)
                    if resp.status_code in (200, 201):
                        _new = self._fetch_inbound_detail(inbound_id)
                        try:
                            _s = _json.loads(_new.get('settings')) if isinstance(_new.get('settings'), str) else (_new.get('settings') or {})
                        except Exception:
                            _s = {}
                        for c2 in (_s.get('clients') or []):
                            if c2.get('email') == username:
                                if proto in ('vless','vmess'):
                                    if c2.get('id') == updated.get('id'):
                                        return updated
                                elif proto == 'trojan':
                                    if c2.get('password') == updated.get('password'):
                                        return updated
                    else:
                        last_preview = f"{ep} -> HTTP {resp.status_code}: {(resp.text or '')[:180]}"
                except requests.RequestException:
                    last_preview = f"{ep} -> EXC clients"
            if last_preview:
                try:
                    logger.error(f"3x-UI rotate update failed: {last_preview}")
                except Exception:
                    pass
            return None
        except Exception:
            return None


def VpnPanelAPI(panel_id: int) -> BasePanelAPI:
    panel_row = query_db("SELECT * FROM panels WHERE id = ?", (panel_id,), one=True)
    if not panel_row:
        raise ValueError(f"Panel with ID {panel_id} not found in database.")
    ptype = (panel_row.get('panel_type') or 'marzban').lower()
    if ptype == 'marzban':
        return MarzbanAPI(panel_row)
    if ptype == 'marzneshin':
        return MarzneshinAPI(panel_row)
    if ptype in ('xui', 'x-ui', 'sanaei', 'alireza'):
        return XuiAPI(panel_row)
    if ptype in ('3xui', '3x-ui', '3x ui'):
        return ThreeXuiAPI(panel_row)
    if ptype in ('txui', 'tx-ui', 'tx ui', 'tx'):
        return TxUiAPI(panel_row)
    logger.error(f"Unknown panel type '{ptype}' for panel {panel_row['name']}")
    return MarzbanAPI(panel_row)