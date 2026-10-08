"""Regression checks: isolated files and local subprocesses, no remote access."""
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from src.config_manager import ConfigManager, TunnelConfig
from src.net_probes import NetworkProbes
from src.system_helper import SystemHelper
from src.tunnel_daemon import TunnelDaemon

REAL_POPEN=subprocess.Popen

class ConfigTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); self.path=Path(self.temp.name)/'config.json'; self.mgr=ConfigManager(str(self.path))
 def tearDown(self): self.temp.cleanup()
 def test_no_write_on_first_load(self): self.assertFalse(self.path.exists())
 def test_defaults(self):
  cfg=TunnelConfig()
  self.assertEqual(cfg.validate(),{})
  self.assertEqual(cfg.server_host,"server.example.com")
  self.assertEqual(cfg.username,"user")
  self.assertFalse(cfg.auto_connect_on_start)
 def test_host_argument_injection(self):
  for value in ['-oProxyCommand=calc','host; touch x','host\nname','999.300.1.1','fe80::1%bad name']:
   with self.subTest(value=value): self.assertIn('server_host',replace(TunnelConfig(),server_host=value).validate())
 def test_valid_hosts(self):
  for value in ['example.com','127.0.0.1','::1','2001:db8::1']:
   with self.subTest(value=value): self.assertNotIn('server_host',replace(TunnelConfig(),server_host=value).validate())
 def test_user_injection(self):
  for value in ['user;id','user@host','-root','root\n']:
   self.assertIn('username',replace(TunnelConfig(),username=value).validate())
 def test_numeric_types(self):
  for value in [True,0,-1,'22',65536,2.5,None]:
   self.assertIn('server_port',replace(TunnelConfig(),server_port=value).validate())
 def test_policy_and_timing_validation(self):
  cfg=replace(TunnelConfig(),host_key_policy='no',reconnect_delay=-1,keepalive_interval=0,auto_reconnect='false')
  self.assertEqual(set(cfg.validate()),{'host_key_policy','reconnect_delay','keepalive_interval','auto_reconnect'})
 def test_key_path_validation(self):
  self.assertIn('key_file_path',replace(TunnelConfig(),auth_mode='key_file',key_file_path='relative.key').validate())
 def test_old_numeric_strings(self):
  self.path.write_text(json.dumps({'server_port':'2222','local_port':'7897'}),encoding='utf-8')
  cfg=ConfigManager(str(self.path)).config; self.assertEqual(cfg.server_port,2222)
 def test_profiles_and_preservation(self):
  cfg=replace(TunnelConfig(),profile_name='实验室 B',mask_logs=True,reconnect_delay=17,keepalive_interval=23)
  self.assertTrue(self.mgr.save(cfg)); self.assertTrue(self.mgr.save(replace(cfg,local_port=8888)))
  loaded=ConfigManager(str(self.path)); self.assertEqual(loaded.config.profile_name,'实验室 B')
  self.assertEqual(loaded.config.reconnect_delay,17); self.assertTrue(loaded.config.mask_logs)
  self.assertEqual(loaded.config.keepalive_interval,23)
  self.assertIsNotNone(loaded.switch_profile('默认配置')); self.assertTrue(loaded.delete_profile('实验室 B'))
  self.assertFalse(loaded.delete_profile('默认配置'))
 def test_save_failure_transaction(self):
  self.assertTrue(self.mgr.save(TunnelConfig())); before=self.path.read_bytes()
  with patch('src.config_manager.os.replace',side_effect=OSError('simulated disk failure')):
   self.assertFalse(self.mgr.save(replace(TunnelConfig(),profile_name='failed')))
  self.assertEqual(self.path.read_bytes(),before); self.assertNotIn('failed',self.mgr.profiles)
  self.assertEqual(len(list(self.path.parent.glob('tmp*'))),0)
 def test_corrupt_backup(self):
  self.path.write_text('{broken',encoding='utf-8'); mgr=ConfigManager(str(self.path))
  self.assertTrue(mgr.load_warning); self.assertEqual(self.path.read_text(),'{broken')
  self.assertTrue(mgr.save(TunnelConfig())); self.assertEqual(self.path.with_suffix('.invalid.bak').read_text(),'{broken')
 def test_invalid_schema(self):
  for payload in [[],{'profiles':{}},{'profiles':{'a':{'server_port':False}}}]:
   self.path.write_text(json.dumps(payload),encoding='utf-8'); self.assertTrue(ConfigManager(str(self.path)).load_warning)

class SecurityTests(unittest.TestCase):
 def test_ssh_options(self):
  cmd=SystemHelper.ssh_base_command('ssh.exe',TunnelConfig())
  for option in ['BatchMode=yes','StrictHostKeyChecking=accept-new','ForwardAgent=no','ForwardX11=no','PermitLocalCommand=no']:
   self.assertIn(option,cmd)
  self.assertEqual(cmd[1:3],['-F','none'])
  self.assertIn('StrictHostKeyChecking=yes',SystemHelper.ssh_base_command('ssh',replace(TunnelConfig(),host_key_policy='yes')))
 def test_command_rejects_unvalidated_port(self):
  with self.assertRaises(ValueError): SystemHelper.ssh_base_command('ssh',replace(TunnelConfig(),remote_port='22; id'))
 def test_mask(self):
  cfg=replace(TunnelConfig(),key_file_path='C:\\secret\\id_ed25519')
  text=SystemHelper.mask_sensitive_text(f'{cfg.username}@{cfg.server_host} {cfg.key_file_path} 2001:db8::1',cfg)
  for secret in [cfg.username,cfg.server_host,cfg.key_file_path,'2001:db8::1']: self.assertNotIn(secret,text)
 def test_diagnostic_no_destructive_fix(self):
  for error in ['remote port forwarding failed','Host key verification failed','Permission denied']:
   diag=NetworkProbes.diagnose_error(error,17897,'host','user'); self.assertIsNone(diag['action_command']); self.assertTrue(diag['fatal'])
 def test_forward_confirmation_requires_exact_mapping(self):
  cfg=TunnelConfig(); valid='debug1: remote forward success for: listen 127.0.0.1:17897, connect 127.0.0.1:7897'
  self.assertTrue(TunnelDaemon.forwarding_confirmed(valid,cfg))
  for text in [valid.replace('success','failure'),valid.replace('17897','17898'),'Authenticated to host','Entering interactive session']:
   self.assertFalse(TunnelDaemon.forwarding_confirmed(text,cfg))

class E2ETests(unittest.TestCase):
 def run_script(self,script,**kwargs):
  with patch('src.system_helper.subprocess.Popen',side_effect=lambda *a,**kw:REAL_POPEN([sys.executable,'-u','-c',script],**kw)):
   return SystemHelper.test_remote_proxy_e2e('fake-ssh',TunnelConfig(),**kwargs)
 def test_connect_200_tls_failure(self):
  ok,_=self.run_script("import sys; print('HTTP/1.1 200 Connection established'); print('curl: (60) SSL certificate problem',file=sys.stderr); sys.exit(60)")
  self.assertFalse(ok)
 def test_valid_204(self):
  ok,msg=self.run_script("print('__CT_HTTP__:204',end='')"); self.assertTrue(ok); self.assertIn('204',msg)
 def test_api_401_counts_reachability(self):
  ok,msg=self.run_script("print('__CT_HTTP__:401',end='')",target='OpenAI'); self.assertTrue(ok); self.assertIn('凭据',msg)
 def test_proxy_auth_not_success(self):
  ok,_=self.run_script("print('__CT_HTTP__:407',end='')"); self.assertFalse(ok)
 def test_http_marker_does_not_override_failure(self):
  ok,_=self.run_script("import sys; print('__CT_HTTP__:200'); sys.exit(60)"); self.assertFalse(ok)
 def test_output_bound(self):
  ok,msg=self.run_script("import sys; sys.stdout.write('x'*100000); sys.stdout.flush()")
  self.assertFalse(ok); self.assertIn('上限',msg)
 def test_cancellation_reaps_process(self):
  event=threading.Event(); event.set()
  ok,msg=self.run_script('import time; time.sleep(30)',stop_event=event)
  self.assertFalse(ok); self.assertIn('取消',msg)
 def test_timeout(self):
  ok,msg=self.run_script('import time; time.sleep(30)',timeout_sec=0.2)
  self.assertFalse(ok); self.assertIn('超时',msg)
 def test_target_allowlist(self):
  ok,_=SystemHelper.test_remote_proxy_e2e('fake',TunnelConfig(),target='https://x;id'); self.assertFalse(ok)

class LifecycleTests(unittest.TestCase):
 def test_no_reconnect_when_disabled(self):
  daemon=TunnelDaemon()
  with patch('src.tunnel_daemon.SSHFinder.find_ssh',return_value=('ssh','ok')),patch('src.tunnel_daemon.NetworkProbes.probe_local_proxy',return_value=(False,'offline')) as probe:
   self.assertTrue(daemon.start(replace(TunnelConfig(),auto_reconnect=False))); self.assertTrue(daemon.wait_stopped(2))
   self.assertEqual(probe.call_count,1); self.assertEqual(daemon.current_state,'ERROR')
 def test_cancel_preflight(self):
  entered=threading.Event(); release=threading.Event(); daemon=TunnelDaemon()
  def probe(**kw): entered.set(); release.wait(2); return True,'ok'
  with patch('src.tunnel_daemon.SSHFinder.find_ssh',return_value=('ssh','ok')),patch('src.tunnel_daemon.NetworkProbes.probe_local_proxy',side_effect=probe),patch('src.tunnel_daemon.subprocess.Popen') as popen:
   self.assertTrue(daemon.start(TunnelConfig())); self.assertTrue(entered.wait(1)); self.assertTrue(daemon.is_running())
   daemon.stop(); self.assertEqual(daemon.current_state,'STOPPING'); release.set(); self.assertTrue(daemon.wait_stopped(2))
   popen.assert_not_called(); self.assertEqual(daemon.current_state,'STOPPED')
 def session(self,ready):
  processes=[]; cfg=TunnelConfig(); daemon=TunnelDaemon()
  script="import sys,time; "+("print('debug1: remote forward success for: listen 127.0.0.1:17897, connect 127.0.0.1:7897',file=sys.stderr,flush=True); " if ready else '')+"time.sleep(30)"
  def spawn(*a,**kw):
   proc=REAL_POPEN([sys.executable,'-u','-c',script],**kw); processes.append(proc); return proc
  with patch('src.tunnel_daemon.SSHFinder.find_ssh',return_value=('fake','ok')),patch('src.tunnel_daemon.NetworkProbes.probe_local_proxy',return_value=(True,'ok')),patch('src.tunnel_daemon.NetworkProbes.probe_remote_server',return_value=(True,'ok')),patch('src.tunnel_daemon.subprocess.Popen',side_effect=spawn):
   try:
    self.assertTrue(daemon.start(cfg)); cfg.remote_port=5555
    self.assertFalse(daemon.start(cfg))
    if ready:
     deadline=time.monotonic()+2
     while daemon.current_state!='CONNECTED' and time.monotonic()<deadline: time.sleep(.02)
     self.assertEqual(daemon.current_state,'CONNECTED'); self.assertEqual(daemon._current_config.remote_port,17897)
    else:
     time.sleep(1.7); self.assertNotEqual(daemon.current_state,'CONNECTED')
   finally:
    daemon.stop(); self.assertTrue(daemon.wait_stopped(3))
  self.assertTrue(processes); self.assertTrue(all(p.poll() is not None for p in processes)); self.assertIsNone(daemon._current_process)
 def test_confirmed_session_cleanup(self): self.session(True)
 def test_live_process_does_not_mean_connected(self): self.session(False)
 def test_fatal_failure_stops_retries(self):
  daemon=TunnelDaemon()
  with patch('src.tunnel_daemon.SSHFinder.find_ssh',return_value=('fake','ok')),patch('src.tunnel_daemon.NetworkProbes.probe_local_proxy',return_value=(True,'ok')),patch('src.tunnel_daemon.NetworkProbes.probe_remote_server',return_value=(True,'ok')),patch.object(daemon,'_run_session',return_value=('Permission denied (publickey)',255)) as session:
   daemon.start(TunnelConfig()); self.assertTrue(daemon.wait_stopped(2)); self.assertEqual(session.call_count,1); self.assertEqual(daemon.current_state,'ERROR')
 def test_reconnect_wait_cancellable(self):
  daemon=TunnelDaemon()
  with patch('src.tunnel_daemon.SSHFinder.find_ssh',return_value=('fake','ok')),patch('src.tunnel_daemon.NetworkProbes.probe_local_proxy',return_value=(False,'offline')):
   daemon.start(TunnelConfig()); deadline=time.monotonic()+2
   while daemon.current_state!='RECONNECTING' and time.monotonic()<deadline: time.sleep(.01)
   self.assertEqual(daemon.current_state,'RECONNECTING'); daemon.stop(); self.assertTrue(daemon.wait_stopped(.5))


class ProbeTests(unittest.TestCase):
 def test_dns_timeout_is_bounded(self):
  release=threading.Event()
  def slow(*a,**kw): release.wait(2); return []
  with patch('src.net_probes.socket.getaddrinfo',side_effect=slow):
   try:
    started=time.monotonic(); ok,msg=NetworkProbes.probe_remote_server('host',timeout=.15)
    self.assertFalse(ok); self.assertIn('解析超时',msg); self.assertLess(time.monotonic()-started,.6)
   finally: release.set()
 def test_dns_cancel_is_bounded(self):
  stop=threading.Event(); release=threading.Event()
  def slow(*a,**kw): release.wait(2); return []
  with patch('src.net_probes.socket.getaddrinfo',side_effect=slow):
   try:
    stop.set(); ok,msg=NetworkProbes.probe_remote_server('host',stop_event=stop)
    self.assertFalse(ok); self.assertIn('取消',msg)
   finally: release.set()


@unittest.skipUnless(sys.platform=='win32','Windows-only handles')
class WindowsScopeTests(unittest.TestCase):
 def test_job_kills_descendants(self):
  import ctypes
  from ctypes import wintypes
  from src.process_scope import ProcessScope
  kernel=ctypes.WinDLL('kernel32',use_last_error=True)
  kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]; kernel.OpenProcess.restype=wintypes.HANDLE
  kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]; kernel.WaitForSingleObject.restype=wintypes.DWORD
  kernel.CloseHandle.argtypes=[wintypes.HANDLE]; kernel.CloseHandle.restype=wintypes.BOOL
  script="import subprocess,sys,time; time.sleep(.2); p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);print(p.pid,flush=True);time.sleep(30)"
  parent=REAL_POPEN([sys.executable,'-u','-c',script],stdout=subprocess.PIPE,text=True,creationflags=SystemHelper.creationflags())
  scope=ProcessScope(parent); child_handle=None
  try:
   child_pid=int(parent.stdout.readline())
   child_handle=kernel.OpenProcess(0x100000,False,child_pid)
   self.assertTrue(child_handle)
   scope.close(); parent.wait(timeout=3)
   self.assertEqual(kernel.WaitForSingleObject(child_handle,3000),0)
  finally:
   scope.close(); SystemHelper.terminate_process(parent); parent.stdout.close()
   if child_handle: kernel.CloseHandle(child_handle)
 def test_job_does_not_kill_unassigned_process(self):
  from src.process_scope import ProcessScope
  owned=REAL_POPEN([sys.executable,'-c','import time;time.sleep(30)'],creationflags=SystemHelper.creationflags())
  other=REAL_POPEN([sys.executable,'-c','import time;time.sleep(30)'],creationflags=SystemHelper.creationflags())
  scope=ProcessScope(owned)
  try:
   scope.close(); owned.wait(timeout=3); self.assertIsNone(other.poll())
  finally:
   scope.close(); SystemHelper.terminate_process(owned); SystemHelper.terminate_process(other)
 def test_mutex_blocks_second_instance(self):
  import ctypes
  import uuid
  from ctypes import wintypes
  from main import ensure_single_instance
  handle=None
  try:
   with patch.dict('os.environ',{'USERNAME':'CodexTunnelQA_'+uuid.uuid4().hex}),patch('tkinter.messagebox.showinfo'):
    handle=ensure_single_instance(); self.assertTrue(handle); self.assertFalse(ensure_single_instance())
  finally:
   if handle:
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]; kernel.CloseHandle.restype=wintypes.BOOL; kernel.CloseHandle(handle)

if __name__=='__main__': unittest.main(verbosity=2)
