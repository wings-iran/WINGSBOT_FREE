import requests
import uuid
from datetime import datetime, timedelta
import json
import re
from urllib.parse import urlsplit
from .config import logger
from .db import query_db


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
            for c in clients:
                if c.get('email') == username:
                    # compute renew values
                    current_exp = int(c.get('expiryTime', 0) or 0)
                    target_exp = current_exp
                    if add_ms > 0:
                        base = max(current_exp, now_ms)
                        target_exp = base + add_ms
                    new_total = int(c.get('totalGB', 0) or 0) + (add_bytes if add_bytes > 0 else 0)
                    updated = dict(c)
                    updated['expiryTime'] = target_exp
                    updated['totalGB'] = new_total
                    # send update
                    settings_payload = json.dumps({"clients": [updated]})
                    payload = {"id": int(inbound_id), "settings": settings_payload}
                    for up in ["/xui/API/inbounds/updateClient", "/panel/API/inbounds/updateClient", "/xui/api/inbounds/updateClient", "/panel/api/inbounds/updateClient"]:
                        try:
                            resp = self.session.post(f"{self.base_url}{up}", headers={'Content-Type': 'application/json'}, json=payload, timeout=15)
                            if resp.status_code in (200, 201):
                                # assume success
                                return updated, "Success"
                        except requests.RequestException:
                            continue
                    return None, "به‌روزرسانی کلاینت ناموفق بود"
        return None, "کلاینت برای تمدید یافت نشد"

    async def create_user(self, user_id, plan):
        return None, None, "برای X-UI ابتدا اینباند را انتخاب کنید."


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
            logger.error(f"3x-UI login error: {e}")
            return False

    def list_inbounds(self):
        if not self.get_token():
            return None, "خطا در ورود به پنل 3x-UI"
        try:
            endpoints = [
                f"{self.base_url}/xui/api/inbounds/list",
                f"{self.base_url}/xui/api/inbounds",
            ]
            last_error = None
            for attempt in range(2):  # try once, then re-login and try again
                for url in endpoints:
                    resp = self.session.get(url, headers=self._json_headers, timeout=12)
                    if resp.status_code != 200:
                        last_error = f"HTTP {resp.status_code}"
                        continue
                    ctype = resp.headers.get('content-type', '').lower()
                    body = resp.text or ''
                    if ('application/json' not in ctype) and not (body.strip().startswith('{') or body.strip().startswith('[')):
                        last_error = "پاسخ JSON معتبر نیست (ممکن است هنوز لاگین نشده باشد)"
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
                            # fallback: first list-like value
                            for k, v in data.items():
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
                # retry: re-login once if first pass failed
                if attempt == 0:
                    self.get_token()
            if last_error:
                logger.error(f"3x-UI list_inbounds error: {last_error}")
                return None, last_error
            return None, "Unknown"
        except requests.RequestException as e:
            logger.error(f"3x-UI list_inbounds error: {e}")
            return None, str(e)

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

        # Only form-encoded OAuth2 Password on /api/admins/token
        last_err = None
        for base in bases:
            for path in ("/api/admins/token", "/api/admins/token/"):
                url = f"{base}{path}"
                try:
                    resp = self.session.post(url, data={"username": self.username, "password": self.password, "grant_type": "password"}, headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}, timeout=12)
                    if resp.status_code not in (200, 201):
                        last_err = f"HTTP {resp.status_code} @ {url}"
                        continue
                    try:
                        data = resp.json()
                    except ValueError:
                        last_err = f"non-JSON @ {url}"
                        continue
                    token_val = data.get("access_token") or data.get("token")
                    if isinstance(token_val, str) and token_val:
                        if token_val.lower().startswith("bearer "):
                            token_val = token_val[7:].strip()
                        self.token = token_val.strip()
                        self._last_token_error = None
                        return True
                    last_err = f"no access_token in response @ {url}"
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