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
    """Marzneshin support.
    - If token is present: use {BASE}/app/apiv2 endpoints with Token header
    - Else: fallback to cookie-based X-UI uppercase endpoints
    """

    def __init__(self, panel_row):
        self.panel_id = panel_row['id']
        self.base_url = panel_row['url'].rstrip('/')
        self.api_base = self.base_url if '/app' in self.base_url else f"{self.base_url}/app"
        self.username = panel_row.get('username')
        self.password = panel_row.get('password')
        self.token = (panel_row.get('token') or '').strip()
        self.sub_base = (panel_row.get('sub_base') or '').strip().rstrip('/') if isinstance(panel_row, dict) else ''
        self.session = requests.Session()
        self._json_headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if self.token:
            self._json_headers['Token'] = self.token

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
            if self.token:
                url = f"{self.api_base}/apiv2/inbounds"
                resp = self.session.get(url, headers={'Accept': 'application/json', 'Token': self.token}, timeout=12)
                if resp.status_code != 200:
                    return None, f"HTTP {resp.status_code} @ {url}"
                try:
                    data = resp.json()
                except ValueError:
                    body = resp.text or ''
                    return None, f"پاسخ JSON معتبر نیست @ {url} :: {body[:200]}"
                items = self._find_first_list_of_dicts(data)
                if not isinstance(items, list):
                    return None, "لیست اینباند نامعتبر است"
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
            # Fallback to cookie-based X-UI
            if not self.username or not self.password:
                return None, "اطلاعات ورود پنل تنظیم نشده است"
            login = self.session.post(f"{self.base_url}/login", json={"username": self.username, "password": self.password}, headers=self._json_headers, timeout=12)
            login.raise_for_status()
            resp = self.session.get(f"{self.base_url}/xui/API/inbounds/", headers={'Accept': 'application/json'}, timeout=12)
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
            if self.token:
                ep = f"{self.api_base}/apiv2/inbounds/addClient"
                payload = {"id": int(inbound_id), "settings": json.dumps(settings_obj)}
                resp = self.session.post(ep, data=json.dumps(payload).encode('utf-8'), headers={'Accept': 'application/json', 'Content-Type': 'application/json', 'Token': self.token}, timeout=15)
                if resp.status_code not in (200, 201):
                    return None, None, f"HTTP {resp.status_code} @ {ep}: {(resp.text or '')[:200]}"
                try:
                    data = resp.json()
                except ValueError:
                    return None, None, f"non-JSON response @ {ep}: {(resp.text or '')[:200]}"
                ok = isinstance(data, dict) and (
                    data.get('success') is True or
                    str(data.get('status','')).lower() in ('ok','success','200') or
                    str(data.get('code','')).startswith('2') or
                    ('msg' in data and isinstance(data['msg'], str) and 'success' in data['msg'].lower())
                )
                if not ok:
                    return None, None, f"API failure @ {ep}: {(resp.text or '')[:200]}"
                # success
                origin = self.sub_base or f"{urlsplit(self.base_url).scheme}://{urlsplit(self.base_url).hostname}{(':'+str(urlsplit(self.base_url).port)) if urlsplit(self.base_url).port and not ((urlsplit(self.base_url).scheme=='http' and urlsplit(self.base_url).port==80) or (urlsplit(self.base_url).scheme=='https' and urlsplit(self.base_url).port==443)) else ''}"
                sub_link = f"{origin}/sub/{subid}"
                return f"user_{subid}", sub_link, "Success"
            # Fallback cookie-based X-UI
            if not self.username or not self.password:
                return None, None, "اطلاعات ورود پنل تنظیم نشده است"
            login = self.session.post(f"{self.base_url}/login", json={"username": self.username, "password": self.password}, headers=self._json_headers, timeout=12)
            login.raise_for_status()
            ep = f"{self.base_url}/xui/API/inbounds/addClient"
            payload = {"id": int(inbound_id), "settings": json.dumps(settings_obj)}
            resp = self.session.post(ep, json=payload, headers={'Content-Type': 'application/json'}, timeout=15)
            if resp.status_code not in (200, 201):
                return None, None, f"HTTP {resp.status_code}: {(resp.text or '')[:200]}"
            origin = self.sub_base or f"{urlsplit(self.base_url).scheme}://{urlsplit(self.base_url).hostname}{(':'+str(urlsplit(self.base_url).port)) if urlsplit(self.base_url).port and not ((urlsplit(self.base_url).scheme=='http' and urlsplit(self.base_url).port==80) or (urlsplit(self.base_url).scheme=='https' and urlsplit(self.base_url).port==443)) else ''}"
            sub_link = f"{origin}/sub/{subid}?name={subid}"
            return f"user_{subid}", sub_link, "Success"
        except requests.RequestException as e:
            logger.error(f"Marzneshin create_user_on_inbound error: {e}")
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