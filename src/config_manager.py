"""Validated profiles and transactional configuration persistence."""
import hashlib
import ipaddress
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace, field
from pathlib import Path
from typing import Dict, Optional
import uuid
import copy

@dataclass
class ForwardRule:
    direction: str = "L"
    listen_port: int = 8888
    target_host: str = "127.0.0.1"
    target_port: int = 8888
    enabled: bool = True
    name: str = "端口转发"

    def validate(self):
        errors=[]
        if self.direction not in ("L","R"): errors.append("转发方向须为 L 或 R")
        for value in (self.listen_port,self.target_port):
            if type(value) is not int or not 1<=value<=65535: errors.append("转发端口须为 1–65535")
        if type(self.enabled) is not bool: errors.append("规则启用状态无效")
        if not isinstance(self.name,str) or not 1<=len(self.name)<=60 or any(ord(c)<32 for c in self.name): errors.append("规则名称无效")
        host_errors=TunnelConfig(server_host=self.target_host).validate(check_key=False)
        if 'server_host' in host_errors: errors.append("转发目标须为合法 IP 或域名")
        return errors

    def argument(self):
        if self.validate(): raise ValueError("；".join(self.validate()))
        host=f"[{self.target_host}]" if ':' in self.target_host else self.target_host
        return f"127.0.0.1:{self.listen_port}:{host}:{self.target_port}"

@dataclass
class AppPreferences:
    theme_mode: str = "light"
    minimize_to_tray: bool = True
    run_at_startup: bool = False
    start_in_tray: bool = False
    mask_logs: bool = True
    notifications: bool = False
    notification_delay: int = 20
    notification_cooldown: int = 120
    quiet_start: int = 23
    quiet_end: int = 8
    quiet_enabled: bool = False
    probe_enabled: bool = True
    probe_interval: int = 90
    probe_failures: int = 3
    network_watch: bool = True
    startup_delay: int = 5

    def validate(self):
        if self.theme_mode not in ('light','dark','system'): raise ValueError('主题无效')
        for key, value in asdict(self).items():
            default=getattr(AppPreferences(),key)
            if type(value) is not type(default): raise ValueError(f'{key} 类型无效')
        for key,lo,hi in [('notification_delay',5,300),('notification_cooldown',30,3600),('quiet_start',0,23),('quiet_end',0,23),('probe_interval',30,600),('probe_failures',1,10),('startup_delay',0,120)]:
            if not lo<=getattr(self,key)<=hi: raise ValueError(f'{key} 范围须为 {lo}–{hi}')

@dataclass
class TunnelConfig:
    profile_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    forward_rules: list = field(default_factory=list)
    include_proxy_forward: bool = True
    profile_name: str = "默认配置"
    server_host: str = "server.example.com"
    server_port: int = 22
    username: str = "user"
    auth_mode: str = "system"
    key_file_path: str = ""
    local_port: int = 7897
    remote_port: int = 17897
    keepalive_interval: int = 15
    keepalive_count_max: int = 6
    auto_reconnect: bool = True
    reconnect_delay: int = 5
    connect_timeout: int = 10
    minimize_to_tray: bool = True
    auto_connect_on_start: bool = False
    run_at_startup: bool = False
    mask_logs: bool = True
    theme_mode: str = "light"
    host_key_policy: str = "accept-new"

    def validate(self, check_key: bool = True) -> Dict[str, str]:
        errors = {}
        if not isinstance(self.profile_id,str) or not re.fullmatch(r'[a-f0-9]{32}',self.profile_id): errors['profile_id']='配置 ID 无效'
        if type(self.include_proxy_forward) is not bool: errors['include_proxy_forward']='默认代理转发状态无效'
        if not isinstance(self.forward_rules,list) or len(self.forward_rules)>16: errors['forward_rules']='最多支持 16 条附加规则'
        else:
            seen=set()
            if self.include_proxy_forward: seen.add(('R',self.remote_port))
            for rule in self.forward_rules:
                if not isinstance(rule,ForwardRule): errors['forward_rules']='转发规则格式无效'; break
                issues=rule.validate()
                if issues: errors['forward_rules']='；'.join(issues); break
                if rule.enabled:
                    endpoint=(rule.direction,rule.listen_port)
                    if endpoint in seen: errors['forward_rules']='监听端口重复'; break
                    seen.add(endpoint)
            if not self.include_proxy_forward and not any(isinstance(r,ForwardRule) and r.enabled for r in self.forward_rules): errors['forward_rules']='至少启用一条转发规则'

        if not isinstance(self.profile_name, str) or not self.profile_name.strip() or len(self.profile_name) > 60 or any(ord(c) < 32 for c in self.profile_name):
            errors['profile_name'] = '配置名称须为 1–60 个可见字符'
        host = self.server_host
        if not isinstance(host, str) or not host or host != host.strip() or '%' in host:
            errors['server_host'] = '服务器地址不能为空或包含前后空格'
        else:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                labels = host.rstrip('.').split('.')
                if len(host) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', x) for x in labels):
                    errors['server_host'] = '请输入合法 IP 或主机名（不支持 SSH 配置别名）'
                elif re.fullmatch(r'[0-9.]+', host):
                    errors['server_host'] = 'IPv4 地址格式不正确'
        if not isinstance(self.username, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', self.username):
            errors['username'] = '用户名格式无效，不能以横杠开头或包含空格'
        for field, label, low, high in [
            ('server_port','SSH 端口',1,65535), ('local_port','本地端口',1,65535), ('remote_port','远程端口',1,65535),
            ('keepalive_interval','心跳间隔',1,300), ('keepalive_count_max','心跳容错次数',1,20),
            ('reconnect_delay','重连间隔',1,120), ('connect_timeout','连接超时',3,60),
        ]:
            value = getattr(self, field)
            if type(value) is not int or not low <= value <= high:
                errors[field] = f'{label}须为 {low}–{high} 的整数'
        for field in ('auto_reconnect','minimize_to_tray','auto_connect_on_start','run_at_startup','mask_logs'):
            if type(getattr(self, field)) is not bool:
                errors[field] = f'{field} 必须为布尔值'
        if self.auth_mode not in ('system','key_file'):
            errors['auth_mode'] = '请选择有效认证方式'
        if self.theme_mode not in ('light','dark','system'):
            errors['theme_mode'] = '主题设置无效'
        if self.host_key_policy not in ('accept-new','yes'):
            errors['host_key_policy'] = '主机指纹策略只能为首次信任或严格校验'
        if not isinstance(self.key_file_path, str) or any(c in self.key_file_path for c in ('\x00','\r','\n')):
            errors['key_file_path'] = '私钥路径格式无效'
        elif self.auth_mode == 'key_file':
            if not self.key_file_path or not os.path.isabs(self.key_file_path):
                errors['key_file_path'] = '指定私钥须使用完整文件路径'
            elif check_key and not os.path.isfile(self.key_file_path):
                errors['key_file_path'] = '私钥文件不存在，请重新选择'
        return errors

class ConfigManager:
    def __init__(self, custom_path: Optional[str] = None):
        self.config_path = str(Path(custom_path).resolve()) if custom_path else str(Path(os.getenv('APPDATA') or Path.home()) / 'CodexTunnel' / 'config.json')
        self.preferences = AppPreferences()
        self.profiles: Dict[str, TunnelConfig] = {}
        self.active_profile_name = '默认配置'
        self.last_error = ''
        self.load_warning = ''
        self._load_failed = False
        self._loaded_schema = 3
        self.config = self.load()

    def _dict_to_config(self, data: dict, name: str) -> TunnelConfig:
        if not isinstance(data, dict):
            raise ValueError('配置内容须为对象')
        defaults = asdict(TunnelConfig())
        values = {key: data.get(key, default) for key, default in defaults.items()}
        values['profile_name'] = name
        for key, default in defaults.items():
            if type(default) is int and isinstance(values[key], str) and values[key].isdigit():
                values[key] = int(values[key])
        rules=values.get('forward_rules',[])
        if not isinstance(rules,list) or len(rules)>16: raise ValueError('转发规则数量无效')
        values['forward_rules']=[ForwardRule(**r) if isinstance(r,dict) else r for r in rules]
        cfg = TunnelConfig(**values)
        errors = cfg.validate(check_key=False)
        if errors:
            raise ValueError('；'.join(errors.values()))
        return cfg

    def load(self) -> TunnelConfig:
        path = Path(self.config_path)
        if path.exists():
            try:
                if path.stat().st_size > 1024 * 1024:
                    raise ValueError('配置文件过大')
                data = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(data, dict):
                    raise ValueError('配置根节点须为对象')
                if type(data.get('schema_version',2)) is not int or data.get('schema_version',2) not in (2,3): raise ValueError('不支持的配置版本')
                self._loaded_schema=data.get('schema_version',2)
                if 'preferences' in data:
                    self.preferences=AppPreferences(**data['preferences']); self.preferences.validate()
                if 'profiles' in data:
                    profiles = data['profiles']
                    if not isinstance(profiles, dict) or not 1 <= len(profiles) <= 100:
                        raise ValueError('配置预设数量须为 1–100')
                    loaded = {name: self._dict_to_config(value, name) for name, value in profiles.items()}
                    active = data.get('active_profile')
                    self.active_profile_name = active if isinstance(active, str) and active in loaded else next(iter(loaded))
                else:
                    loaded = {'默认配置': self._dict_to_config(data, '默认配置')}
                    self.active_profile_name = '默认配置'
                if 'preferences' not in data:
                    active_cfg=loaded[self.active_profile_name]
                    self.preferences=AppPreferences(**{k:getattr(active_cfg,k) for k in ('theme_mode','minimize_to_tray','run_at_startup','mask_logs')})
                ids=[c.profile_id for c in loaded.values()]
                if len(ids)!=len(set(ids)): raise ValueError('配置 ID 重复')
                self.profiles = loaded
                return copy.deepcopy(loaded[self.active_profile_name])
            except (ValueError, TypeError, OSError) as exc:
                self.preferences = AppPreferences()
                self.load_warning = f'配置读取失败，暂用默认值；原文件将在保存前备份。原因：{exc}'
                self._load_failed = True
        cfg = TunnelConfig()
        self.profiles = {cfg.profile_name: replace(cfg)}
        self.active_profile_name = cfg.profile_name
        return cfg

    def _commit(self, profiles: Dict[str, TunnelConfig], active: str) -> bool:
        temp_name = None
        try:
            path = Path(self.config_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            if self._load_failed and path.exists():
                backup = path.with_suffix('.invalid.bak')
                index = 1
                while backup.exists():
                    backup = path.with_suffix(f'.invalid.{index}.bak')
                    index += 1
                backup.write_bytes(path.read_bytes())
            self.preferences.validate()
            ids=[c.profile_id for c in profiles.values()]
            if len(ids)!=len(set(ids)): raise ValueError('配置 ID 重复')
            if any(c.validate(check_key=False) for c in profiles.values()): raise ValueError('配置内容无效')
            if self._loaded_schema<3 and path.exists() and not self._load_failed:
                original=path.read_bytes()
                backup=path.with_suffix('.v2-'+hashlib.sha256(original).hexdigest()[:12]+'.bak')
                if not backup.exists(): backup.write_bytes(original)
            payload = {'schema_version':3, 'preferences':asdict(self.preferences), 'active_profile':active, 'profiles':{n:asdict(c) for n,c in profiles.items()}}
            with tempfile.NamedTemporaryFile('w',dir=path.parent,delete=False,encoding='utf-8') as handle:
                temp_name = handle.name
                json.dump(payload,handle,indent=2,ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name,path)
            self.profiles = copy.deepcopy(profiles)
            self.active_profile_name = active
            self.config = copy.deepcopy(profiles[active])
            self.last_error = ''
            self._load_failed = False
            self._loaded_schema = 3
            return True
        except (OSError,ValueError,TypeError) as exc:
            self.last_error = str(exc)
            return False
        finally:
            if temp_name and os.path.exists(temp_name):
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    def save(self, config: TunnelConfig) -> bool:
        errors = config.validate(check_key=False)
        if errors:
            self.last_error = '；'.join(errors.values())
            return False
        profiles = dict(self.profiles)
        profiles[config.profile_name] = copy.deepcopy(config)
        if len(profiles) > 100:
            self.last_error = '最多支持 100 个配置预设'
            return False
        return self._commit(profiles,config.profile_name)

    def switch_profile(self,name: str) -> Optional[TunnelConfig]:
        if name in self.profiles and self._commit(self.profiles,name):
            return copy.deepcopy(self.config)
        return None

    def delete_profile(self,name: str) -> bool:
        if len(self.profiles) <= 1 or name not in self.profiles:
            self.last_error = '至少保留一个配置预设'
            return False
        profiles = {n:c for n,c in self.profiles.items() if n != name}
        active = self.active_profile_name if self.active_profile_name in profiles else next(iter(profiles))
        return self._commit(profiles,active)

    def save_preferences(self, prefs):
        prefs.validate()
        old=self.preferences
        self.preferences=replace(prefs)
        if self._commit(self.profiles,self.active_profile_name): return True
        self.preferences=old
        return False

    def export_profiles(self, names):
        selected={}
        omit={'profile_id','key_file_path','run_at_startup','auto_connect_on_start','minimize_to_tray','theme_mode','mask_logs'}
        for name in names:
            cfg=self.profiles[name]
            row={k:v for k,v in asdict(cfg).items() if k not in omit}
            row['auth_mode']='system'
            selected[name]=row
        return {'format':'CodexTunnelShare','schema_version':1,'profiles':selected}

    def preview_import(self, path):
        source=Path(path)
        if source.stat().st_size>1024*1024: raise ValueError('分享文件超过 1 MiB')
        data=json.loads(source.read_text(encoding='utf-8-sig'))
        if not isinstance(data,dict) or set(data)!={'format','schema_version','profiles'} or data['format']!='CodexTunnelShare' or type(data['schema_version']) is not int or data['schema_version']!=1: raise ValueError('分享格式或版本无效')
        rows=data['profiles']
        if not isinstance(rows,dict) or not 1<=len(rows)<=100: raise ValueError('分享配置数量无效')
        allowed=set(asdict(TunnelConfig()))-{'profile_id','key_file_path','run_at_startup','auto_connect_on_start','minimize_to_tray','theme_mode','mask_logs'}
        result={}
        for name,row in rows.items():
            if not isinstance(row,dict) or set(row)-allowed: raise ValueError('分享包含不允许的字段')
            if row.get('auth_mode','system')!='system': raise ValueError('分享不能携带密钥引用')
            cfg=self._dict_to_config(row,name)
            cfg.auto_connect_on_start=False; cfg.run_at_startup=False; cfg.key_file_path=''
            candidate=name; index=2
            while candidate in self.profiles or candidate in result:
                candidate=name[:52]+f' ({index})'; index+=1
            cfg.profile_name=candidate; result[candidate]=cfg
        return result

    def import_profiles(self, profiles):
        combined=dict(self.profiles)
        for name,cfg in profiles.items():
            if name in combined or cfg.validate(check_key=False): raise ValueError('导入配置冲突或无效')
            combined[name]=copy.deepcopy(cfg)
        if len(combined)>100: raise ValueError('总配置数超过 100')
        return self._commit(combined,self.active_profile_name)
