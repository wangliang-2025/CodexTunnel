"""Workspace regressions use temporary files, loopback servers and isolated subprocesses."""
import copy,json,os,subprocess,sys,tempfile,threading,time,unittest,uuid
from pathlib import Path
from dataclasses import asdict,replace
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.config_manager import ConfigManager,TunnelConfig,ForwardRule,AppPreferences
from src.tunnel_daemon import TunnelDaemon
from src.tunnel_manager import TunnelManager
from src.services import CommandRunner,ClashClient,MetricsService
from src.desktop_services import NetworkWatcher,NotificationGate,KeyService
from src.system_helper import TEST_TARGETS

class WorkspaceConfigTests(unittest.TestCase):
 def setUp(self):self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'config.json';self.mgr=ConfigManager(str(self.path))
 def tearDown(self):self.temp.cleanup()
 def test_preferences_survive_profile_switch(self):
  prefs=replace(AppPreferences(),theme_mode='dark',probe_interval=120,notifications=True)
  self.assertTrue(self.mgr.save_preferences(prefs))
  self.assertTrue(self.mgr.save(replace(TunnelConfig(),profile_name='B')))
  self.mgr.switch_profile('默认配置');loaded=ConfigManager(str(self.path));self.assertEqual(loaded.preferences,prefs)
 def test_preference_failed_save_rolls_back(self):
  old=copy.deepcopy(self.mgr.preferences)
  with patch('src.config_manager.os.replace',side_effect=OSError('disk')):self.assertFalse(self.mgr.save_preferences(replace(old,notifications=True)))
  self.assertEqual(self.mgr.preferences,old)
 def test_legacy_migration(self):
  self.path.write_text(json.dumps({'theme_mode':'dark','minimize_to_tray':False,'run_at_startup':True}),encoding='utf-8')
  mgr=ConfigManager(str(self.path));self.assertEqual(mgr.preferences.theme_mode,'dark');self.assertTrue(mgr.preferences.run_at_startup)
  self.assertTrue(mgr.save(mgr.config));self.assertEqual(ConfigManager(str(self.path)).config.profile_id,mgr.config.profile_id)
 def test_corrupt_preferences_restore_defaults(self):
  self.path.write_text(json.dumps({'schema_version':3,'preferences':{'theme_mode':'invalid'},'profiles':{'x':{}}}),encoding='utf-8')
  mgr=ConfigManager(str(self.path));self.assertTrue(mgr.load_warning);self.assertEqual(mgr.preferences,AppPreferences())
 def test_preferences_reject_wrong_types_and_ranges(self):
  for data in [{'probe_interval':True},{'probe_interval':2},{'quiet_start':24},{'notifications':'yes'},{'startup_delay':121}]:
   with self.subTest(data=data),self.assertRaises(ValueError):replace(AppPreferences(),**data).validate()
 def test_rules_validation(self):
  for rule in [ForwardRule(direction='D'),ForwardRule(listen_port=True),ForwardRule(target_host='host;id'),ForwardRule(target_port=0),ForwardRule(name='x\n')]:self.assertTrue(rule.validate())
  self.assertFalse(ForwardRule(target_host='::1').validate());self.assertEqual(ForwardRule(target_host='::1').argument(),'127.0.0.1:8888:[::1]:8888')
 def test_listeners_duplicate_and_no_rules(self):
  self.assertIn('forward_rules',replace(TunnelConfig(),include_proxy_forward=False).validate())
  self.assertIn('forward_rules',replace(TunnelConfig(),forward_rules=[ForwardRule(direction='R',listen_port=17897)]).validate())
  self.assertIn('forward_rules',replace(TunnelConfig(),forward_rules=[ForwardRule(),ForwardRule()]).validate())
  self.assertFalse(replace(TunnelConfig(),include_proxy_forward=False,forward_rules=[ForwardRule()]).validate())
 def test_rules_roundtrip_deep_copy(self):
  cfg=replace(TunnelConfig(),forward_rules=[ForwardRule()]);self.assertTrue(self.mgr.save(cfg));cfg.forward_rules[0].target_port=9999
  self.assertEqual(self.mgr.config.forward_rules[0].target_port,8888)
  self.assertEqual(ConfigManager(str(self.path)).config.forward_rules[0].target_port,8888)
 def test_duplicate_identity_rejected(self):
  self.assertFalse(self.mgr.save(replace(self.mgr.config,profile_name='duplicate')))
 def test_export_has_no_secrets_or_startup(self):
  cfg=replace(self.mgr.config,key_file_path='C:/secret/private.key',auto_connect_on_start=True,run_at_startup=True)
  self.mgr.save(cfg);payload=self.mgr.export_profiles([cfg.profile_name]);text=json.dumps(payload)
  for field in ('key_file_path','profile_id','auto_connect_on_start','run_at_startup'):self.assertNotIn(field,text)
  self.assertNotIn('private.key',text)
 def test_import_new_identity_no_autoconnect(self):
  original=self.mgr.config;source=Path(self.temp.name)/'share.json';source.write_text(json.dumps(self.mgr.export_profiles([original.profile_name])),encoding='utf-8')
  preview=self.mgr.preview_import(source);cfg=next(iter(preview.values()));self.assertNotEqual(cfg.profile_id,original.profile_id);self.assertFalse(cfg.auto_connect_on_start)
  self.assertNotEqual(cfg.profile_name,original.profile_name);self.assertTrue(self.mgr.import_profiles(preview))
 def test_import_unknown_secret_fields_rejected(self):
  source=Path(self.temp.name)/'share.json'
  for field,value in [('token','secret'),('key_file_path','C:/secret'),('auto_connect_on_start',True),('command','calc')]:
   data={'format':'CodexTunnelShare','schema_version':1,'profiles':{'x':{field:value}}};source.write_text(json.dumps(data),encoding='utf-8')
   with self.subTest(field=field),self.assertRaises(ValueError):self.mgr.preview_import(source)
 def test_import_bad_schema_or_size(self):
  source=Path(self.temp.name)/'share.json'
  for version in [True,2,'1']:
   source.write_text(json.dumps({'format':'CodexTunnelShare','schema_version':version,'profiles':{'x':{}}}),encoding='utf-8')
   with self.assertRaises(ValueError):self.mgr.preview_import(source)
  source.write_bytes(b' '*1048577)
  with self.assertRaises(ValueError):self.mgr.preview_import(source)

class FakeDaemon:
 def __init__(self,**kwargs):self.current_state='STOPPED';self._stop_event=threading.Event();self._current_config=None;self.run_id=0;self.health={};self.connect_start_time=None;self.total_connected_seconds=0;self.retry_count=0;self.session_count=0
 def start(self,cfg):self._current_config=copy.deepcopy(cfg);self._stop_event.clear();self.current_state='CONNECTED';self.run_id+=1;return True
 def is_running(self):return self.current_state=='CONNECTED'
 def stop(self):self._stop_event.set();self.current_state='STOPPED'
 def request_network_check(self):self.network_requested=True
 def _health(self,**kwargs):self.health.update(kwargs)
 rules=staticmethod(TunnelDaemon.rules)

class ManagerTests(unittest.TestCase):
 def setUp(self):
  self.events=[];self.patcher=patch('src.tunnel_manager.TunnelDaemon',FakeDaemon);self.patcher.start()
  self.manager=TunnelManager(replace(AppPreferences(),probe_enabled=False),lambda *v:self.events.append(v),max_connections=2)
  self.a=TunnelConfig();self.b=replace(TunnelConfig(),profile_name='B',server_host='192.0.2.2')
 def tearDown(self):self.manager.close();self.manager._thread.join(2);self.patcher.stop()
 def test_independent_stop(self):
  self.assertTrue(self.manager.start(self.a)[0]);self.assertTrue(self.manager.start(self.b)[0]);self.manager.stop(self.a.profile_id)
  self.assertFalse(self.manager.get(self.a).is_running());self.assertTrue(self.manager.get(self.b).is_running())
 def test_global_local_port_conflict(self):
  a=replace(self.a,forward_rules=[ForwardRule()]);b=replace(self.b,forward_rules=[ForwardRule()])
  self.assertTrue(self.manager.start(a)[0]);self.assertFalse(self.manager.start(b)[0])
 def test_remote_same_host_port_conflict(self):
  self.manager.start(self.a);self.assertFalse(self.manager.start(replace(self.b,server_host=self.a.server_host))[0])
 def test_concurrent_limit(self):
  self.manager.start(self.a);self.manager.start(self.b)
  self.assertFalse(self.manager.start(replace(TunnelConfig(),server_host='192.0.2.3'))[0])
 def test_network_does_not_restart_stopped(self):
  self.manager.start(self.a);self.manager.start(self.b);self.manager.stop(self.a.profile_id);self.manager.network_changed()
  self.assertFalse(hasattr(self.manager.get(self.a),'network_requested'));self.assertTrue(self.manager.get(self.b).network_requested)
 def test_probe_threshold_no_ssh_restart(self):
  self.manager.start(self.a);daemon=self.manager.get(self.a)
  with patch('src.tunnel_manager.MetricsService.remote_listener',return_value={'17897':True}),patch('src.tunnel_manager.MetricsService.latency',return_value={'ok':False,'http':503,'total_ms':20}):
   for i in range(3):self.manager._probe(self.a.profile_id,self.a,daemon.run_id,threading.Event())
  self.assertTrue(self.manager.health[self.a.profile_id]['degraded']);self.assertTrue(daemon.is_running());self.assertEqual(daemon.run_id,1)
 def test_unavailable_listener_does_not_skip_business(self):
  self.manager.start(self.a);daemon=self.manager.get(self.a)
  with patch('src.tunnel_manager.MetricsService.remote_listener',side_effect=RuntimeError('Python missing')),patch('src.tunnel_manager.MetricsService.latency',return_value={'ok':True,'http':204,'total_ms':20}):self.manager._probe(self.a.profile_id,self.a,daemon.run_id,threading.Event())
  health=self.manager.health[self.a.profile_id];self.assertTrue(health['unavailable']);self.assertTrue(health['business']);self.assertFalse(health['degraded'])
 def test_cancelled_or_stale_probe_ignored(self):
  self.manager.start(self.a);daemon=self.manager.get(self.a);original=dict(self.manager.health[self.a.profile_id]);cancel=threading.Event();cancel.set()
  with patch('src.tunnel_manager.MetricsService.remote_listener',return_value={'17897':True}):self.manager._probe(self.a.profile_id,self.a,daemon.run_id,cancel)
  self.assertEqual(self.manager.health[self.a.profile_id],original)
  with patch('src.tunnel_manager.MetricsService.remote_listener',return_value={'17897':True}),patch('src.tunnel_manager.MetricsService.latency',return_value={'ok':True,'http':204,'total_ms':20}):self.manager._probe(self.a.profile_id,self.a,daemon.run_id-1,threading.Event())
  self.assertEqual(self.manager.health[self.a.profile_id],original)
 def test_manual_probe_with_auto_disabled(self):
  self.assertFalse(self.manager.probe_now(self.a.profile_id));self.manager.start(self.a)
  with patch('src.tunnel_manager.MetricsService.remote_listener',return_value={'17897':True}),patch('src.tunnel_manager.MetricsService.latency',return_value={'ok':True,'http':204,'total_ms':20}):
   self.assertTrue(self.manager.probe_now(self.a.profile_id));deadline=time.monotonic()+2
   while not self.manager.history[self.a.profile_id] and time.monotonic()<deadline:time.sleep(.05)
   self.assertTrue(self.manager.history[self.a.profile_id])

class NotificationTests(unittest.TestCase):
 def test_delay_recovery_and_no_repeat(self):
  gate=NotificationGate();prefs=replace(AppPreferences(),notifications=True)
  self.assertIsNone(gate.update('a','ERROR',prefs,now=0,hour=12));self.assertIsNone(gate.update('a','ERROR',prefs,now=19,hour=12))
  self.assertIsNotNone(gate.update('a','ERROR',prefs,now=20,hour=12));self.assertIsNone(gate.update('a','ERROR',prefs,now=80,hour=12));self.assertEqual(gate.update('a','CONNECTED',prefs,now=81,hour=12),'连接已恢复')
 def test_manual_stop_and_disabled(self):
  gate=NotificationGate();prefs=replace(AppPreferences(),notifications=True);gate.update('a','ERROR',prefs,now=0,hour=12);gate.update('a','ERROR',prefs,now=21,hour=12);gate.update('a','STOPPED',prefs,now=22,hour=12)
  self.assertIsNone(gate.update('a','CONNECTED',prefs,now=23,hour=12))
  self.assertIsNone(gate.update('b','ERROR',AppPreferences(),now=200,hour=12))
 def test_quiet_cross_midnight(self):
  prefs=replace(AppPreferences(),quiet_enabled=True)
  self.assertTrue(NotificationGate.quiet(prefs,23));self.assertTrue(NotificationGate.quiet(prefs,7));self.assertFalse(NotificationGate.quiet(prefs,12))
 def test_cooldown_independent_profiles(self):
  prefs=replace(AppPreferences(),notifications=True);gate=NotificationGate()
  for name in ['a','b']:gate.update(name,'ERROR',prefs,now=0,hour=12);self.assertIsNotNone(gate.update(name,'ERROR',prefs,now=21,hour=12))
  gate.update('a','CONNECTED',prefs,now=22,hour=12);gate.update('a','ERROR',prefs,now=23,hour=12);self.assertIsNone(gate.update('a','ERROR',prefs,now=50,hour=12))

class ServiceTests(unittest.TestCase):
 def test_bounded_command_output(self):
  with self.assertRaisesRegex(RuntimeError,'安全上限'):CommandRunner.run([sys.executable,'-c',"print('x'*50000)"],limit=1024)
 def test_command_cancel_and_timeout(self):
  event=threading.Event();threading.Timer(.2,event.set).start()
  with self.assertRaisesRegex(RuntimeError,'取消'):CommandRunner.run([sys.executable,'-c','import time;time.sleep(20)'],event)
  with self.assertRaisesRegex(RuntimeError,'超时'):CommandRunner.run([sys.executable,'-c','import time;time.sleep(20)'],timeout=.2)
 def test_latency_parser_and_proxy_auth(self):
  line='__CT_METRICS__:204:0.001:0.011:0.021:0.031:0.041'
  with patch('src.services.CommandRunner.remote',return_value=(0,line,'')):
   result=MetricsService.latency(TunnelConfig());self.assertTrue(result['ok']);self.assertAlmostEqual(result['tcp_ms'],10);self.assertAlmostEqual(result['total_ms'],41)
  with patch('src.services.CommandRunner.remote',return_value=(0,line.replace('204','407'),'')):self.assertFalse(MetricsService.latency(TunnelConfig())['ok'])
  with patch('src.services.CommandRunner.remote',return_value=(35,line,'TLS error')),self.assertRaises(RuntimeError):MetricsService.latency(TunnelConfig())
 def test_unknown_latency_target(self):
  with self.assertRaises(ValueError):MetricsService.latency(TunnelConfig(),target='https://evil')
 def test_speed_read_cap_and_cancel(self):
  class Response:
   status=200
   def __init__(self):self.count=0
   def __enter__(self):return self
   def __exit__(self,*v):return False
   def read(self,n):self.count+=n;return b'x'*n
  response=Response()
  class Opener:
   def open(self,*a,**kw):return response
  with patch('src.services.urllib.request.build_opener',return_value=Opener()):
   result=MetricsService.speed(TunnelConfig());self.assertEqual(result['bytes'],1048576);self.assertEqual(response.count,1048576)
   event=threading.Event();event.set()
   with self.assertRaisesRegex(RuntimeError,'取消'):MetricsService.speed(TunnelConfig(),event)
 def test_remote_speed_sample_validation(self):
  bad={'bytes':2097152,'seconds':1,'bytes_per_second':2097152,'limit':1048576}
  with patch('src.services.CommandRunner.remote_python',return_value=bad),self.assertRaises(ValueError):MetricsService.speed(TunnelConfig(),remote=True)
 def test_dashboard_bad_data_rejected(self):
  with patch('src.services.CommandRunner.remote_python',return_value={'memory_total':1}),self.assertRaises(ValueError):MetricsService.dashboard(TunnelConfig())
 def test_forward_confirmation_all_kinds(self):
  self.assertTrue(TunnelDaemon.rule_confirmed('debug1: Local forwarding listening on 127.0.0.1 port 8888.',('L',8888,'127.0.0.1',8888)))
  self.assertFalse(TunnelDaemon.rule_confirmed('Authenticated to host',('L',8888,'127.0.0.1',8888)))
  self.assertTrue(TunnelDaemon.rule_confirmed('debug1: remote forward success for: listen 127.0.0.1:9999, connect localhost:8888',('R',9999,'localhost',8888)))
 def test_timer_accumulates_once(self):
  daemon=TunnelDaemon();daemon.connect_start_time=time.monotonic()-.2;daemon._accumulate();value=daemon.total_connected_seconds;self.assertAlmostEqual(value,.2,places=3);daemon._accumulate();self.assertEqual(value,daemon.total_connected_seconds)
 @unittest.skipUnless(sys.platform=='win32','Windows')
 def test_native_network_subscription_cleanup(self):
  watcher=NetworkWatcher(lambda:None);watcher.start();self.assertFalse(watcher.error);watcher.close();watcher._thread.join(2);self.assertFalse(watcher._thread.is_alive())
 def test_key_never_overwrites(self):
  with tempfile.TemporaryDirectory() as folder:
   target=Path(folder)/'key';target.write_text('existing',encoding='utf-8')
   with self.assertRaises(ValueError):KeyService.generate(target)
   self.assertEqual(target.read_text(),'existing')

class LocalControllerTests(unittest.TestCase):
 def setUp(self):
  self.requests=[];requests=self.requests
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def do_CONNECT(self):self.send_response(200);self.end_headers()
   def do_GET(self):
    requests.append((self.path,self.headers.get('Authorization')))
    if getattr(self.server,'redirect',False):
     self.send_response(302);self.send_header('Location','http://127.0.0.1:1/cross-controller');self.end_headers();return
    if self.path=='/configs':data={'mixed-port':self.server.server_port,'port':0}
    elif self.path=='/version':data={'version':'QA'}
    elif self.path=='/traffic':data={'up':1024,'down':2048}
    else:
     self.send_response(302);self.send_header('Location','http://192.0.2.1/token');self.end_headers();return
    body=(json.dumps(data)+'\n').encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
  self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.client=ClashClient(self.server.server_port,'QA-token')
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join(2)
 def test_read_only_api_and_traffic(self):
  self.assertIn('mixed-port',self.client.get('/configs'));self.assertEqual(self.client.traffic()['down'],2048);self.assertEqual(self.requests[0][1],'Bearer QA-token')
  with self.assertRaises(ValueError):self.client.get('/restart')
 def test_proxy_protocol_and_detect(self):
  self.assertTrue(ClashClient.http_proxy(self.server.server_port))
  with patch.object(ClashClient,'http_proxy',side_effect=lambda p:p==self.server.server_port):
   result=self.client.detect();self.assertEqual(result[0]['port'],self.server.server_port);self.assertIn('Mihomo',result[0]['source'])
 def test_redirect_does_not_forward_secret(self):
  import urllib.error
  self.server.redirect=True
  with self.assertRaises(urllib.error.HTTPError) as context:self.client.get('/configs')
  self.assertEqual(context.exception.code,302);self.assertEqual(len(self.requests),1)
 def test_controller_bounds_and_secret_format(self):
  for port in [True,0,65536,'9090']:
   with self.assertRaises(ValueError):ClashClient(port)
  with self.assertRaises(ValueError):ClashClient(9090,'a\nb')
  with patch.object(self.client,'get',return_value={'up':-1,'down':2}),self.assertRaises(ValueError):self.client.traffic()


REAL_POPEN=subprocess.Popen
class MultiRuleProcessTests(unittest.TestCase):
 def run_session(self,include_proxy,second_confirmation=True):
  cfg=replace(TunnelConfig(),include_proxy_forward=include_proxy,forward_rules=[ForwardRule(listen_port=18888,target_port=8888)])
  daemon=TunnelDaemon();procs=[];commands=[]
  first="print('debug1: remote forward success for: listen 127.0.0.1:17897, connect 127.0.0.1:7897',file=sys.stderr,flush=True);" if include_proxy else ''
  second="print('debug1: Local forwarding listening on 127.0.0.1 port 18888.',file=sys.stderr,flush=True);" if second_confirmation else ''
  script="import sys,time;"+first+"time.sleep(.5);"+second+"time.sleep(30)"
  def spawn(command,**kwargs):
   commands.append(command);proc=REAL_POPEN([sys.executable,'-u','-c',script],**kwargs);procs.append(proc);return proc
  with patch('src.tunnel_daemon.SSHFinder.find_ssh',return_value=('fake-ssh','ok')),patch('src.tunnel_daemon.NetworkProbes.probe_remote_server',return_value=(True,'ok')),patch('src.tunnel_daemon.NetworkProbes.probe_local_proxy',side_effect=(lambda **kw:(True,'ok')) if include_proxy else AssertionError('L-only must not probe a proxy')),patch('src.tunnel_daemon.subprocess.Popen',side_effect=spawn):
   try:
    self.assertTrue(daemon.start(cfg));cfg.forward_rules[0].target_port=9999
    time.sleep(.25);self.assertNotEqual(daemon.current_state,'CONNECTED')
    if second_confirmation:
     deadline=time.monotonic()+2
     while daemon.current_state!='CONNECTED' and time.monotonic()<deadline:time.sleep(.02)
     self.assertEqual(daemon.current_state,'CONNECTED');self.assertEqual(daemon._current_config.forward_rules[0].target_port,8888)
    else:time.sleep(.5);self.assertNotEqual(daemon.current_state,'CONNECTED')
    self.assertIn('-L',commands[0]);self.assertIn('127.0.0.1:18888:127.0.0.1:8888',commands[0])
   finally:daemon.stop();self.assertTrue(daemon.wait_stopped(3))
  self.assertTrue(all(proc.poll() is not None for proc in procs))
 def test_all_rules_required(self):self.run_session(True,True)
 def test_missing_confirmation_never_green(self):self.run_session(True,False)
 def test_local_only_without_proxy(self):self.run_session(False,True)

@unittest.skipUnless(sys.platform=='win32','Windows OpenSSH and ACL')
class NativeKeyWorkflowTests(unittest.TestCase):
 def test_actual_keygen_acl_and_no_passphrase_argument(self):
  commands=[]
  def spawn(command,**kwargs):
   args=list(command)
   if Path(args[0]).name.lower()=='ssh-keygen.exe' and '-t' in args:
    commands.append(list(args));args.extend(['-N','']);kwargs['creationflags']=subprocess.CREATE_NO_WINDOW
   return REAL_POPEN(args,**kwargs)
  with tempfile.TemporaryDirectory() as folder:
   target=Path(folder)/'new-folder'/'key'
   with patch('src.desktop_services.subprocess.Popen',side_effect=spawn):result=KeyService.generate(target)
   self.assertTrue(target.is_file());self.assertTrue(Path(str(target)+'.pub').is_file());self.assertTrue(result['public_key'].startswith('ssh-ed25519 '));self.assertIn('SHA256:',result['fingerprint'])
   self.assertTrue(commands);self.assertNotIn('-N',commands[0]);self.assertNotIn('-P',commands[0])
   checker=Path(folder)/'acl.ps1'
   checker.write_text("param([string]$KeyPath)\n$ErrorActionPreference='Stop'\n$acl=Get-Acl -LiteralPath $KeyPath\n@{sids=@($acl.Access | ForEach-Object {$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value})}|ConvertTo-Json -Compress",encoding='utf-8')
   code,out,err=CommandRunner.run(['powershell.exe','-NoProfile','-NonInteractive','-File',str(checker),'-KeyPath',str(target)],timeout=8)
   self.assertEqual(code,0,err);data=json.loads(out);self.assertTrue(data['sids']);self.assertFalse(set(data['sids']) & {'S-1-1-0','S-1-5-32-545','S-1-5-11'});self.assertTrue(all(sid.startswith('S-1-5-') for sid in data['sids']))
   code,whoami,err=CommandRunner.run(['whoami.exe','/user','/fo','csv','/nh'])
   import re
   owner_sid=re.search(r'S-1-5-(?:\d+-)*\d+',whoami).group()
   self.assertTrue(set(data['sids'])<={owner_sid,'S-1-5-18','S-1-5-32-544'})
   before=target.read_bytes()
   with self.assertRaises(ValueError):KeyService.generate(target)
   self.assertEqual(target.read_bytes(),before)
 def test_public_file_collision_preserved(self):
  with tempfile.TemporaryDirectory() as folder:
   target=Path(folder)/'key';public=Path(str(target)+'.pub');public.write_text('existing public',encoding='utf-8')
   with self.assertRaises(ValueError):KeyService.generate(target)
   self.assertFalse(target.exists());self.assertEqual(public.read_text(),'existing public')

if __name__=='__main__':unittest.main()
