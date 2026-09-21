"""Every test runs on Linux, macOS and Windows with no GPU tools installed: external
commands are faked, except two that spawn this Python to prove the timeout is real."""
import io
import json
import subprocess
import sys
import unittest
from unittest import mock

from tools import hardware_probe as hp


def fake_run(responses):
    """run_bounded stand-in: first argv-prefix match wins; anything else is missing."""
    calls = []

    def run(command, timeout_s=hp.DEFAULT_TIMEOUT_S, encoding='utf-8'):
        calls.append(list(command))
        for prefix, response in responses:
            if command[:len(prefix)] == list(prefix):
                return {'stdout': '', **response}
        return {'status': 'missing', 'stdout': ''}

    run.calls = calls
    return run


class RunBoundedTests(unittest.TestCase):
    def test_missing_executable_is_reported_not_raised(self):
        self.assertEqual(hp.run_bounded(['definitely-not-a-real-tool-xyz'])['status'], 'missing')

    def test_real_timeout_kills_the_command(self):
        result = hp.run_bounded([sys.executable, '-c', 'import time; time.sleep(30)'], timeout_s=0.5)
        self.assertEqual(result, {'status': 'timeout', 'stdout': ''})

    def test_ok_and_failed_statuses(self):
        ok = hp.run_bounded([sys.executable, '-c', 'print("hi")'])
        self.assertEqual((ok['status'], ok['stdout'].strip()), ('ok', 'hi'))
        bad = hp.run_bounded([sys.executable, '-c', 'raise SystemExit(3)'])
        self.assertEqual((bad['status'], bad['returncode']), ('failed', 3))

    def test_os_error_is_contained(self):
        with mock.patch.object(hp.shutil, 'which', return_value='/x/tool'), \
                mock.patch.object(hp.subprocess, 'run', side_effect=PermissionError('/secret/path')):
            result = hp.run_bounded(['tool'])
        self.assertEqual(result, {'status': 'error', 'error': 'PermissionError', 'stdout': ''})

    def test_never_uses_a_shell_or_stdin(self):
        done = subprocess.CompletedProcess([], 0, stdout=b'', stderr=b'')
        with mock.patch.object(hp.shutil, 'which', return_value='/x/tool'), \
                mock.patch.object(hp.subprocess, 'run', return_value=done) as run:
            hp.run_bounded(['tool', '--flag'], timeout_s=2)
        kwargs = run.call_args.kwargs
        self.assertIs(kwargs['shell'], False)
        self.assertEqual(kwargs['stdin'], subprocess.DEVNULL)
        self.assertEqual(kwargs['timeout'], 2)

    def test_decodes_utf16_from_wsl(self):
        text = '  NAME      STATE   VERSION\n* Ubuntu    Running 2\n'
        self.assertEqual(hp.decode_output(b'\xff\xfe' + text.encode('utf-16-le')), text)
        self.assertEqual(hp.decode_output(text.encode('utf-16-le')), text)
        self.assertEqual(hp.decode_output(b'plain\n'), 'plain\n')


class PrivacyTests(unittest.TestCase):
    def test_redact_path(self):
        self.assertEqual(hp.redact_path('/Users/alice/.local/bin/uv', home='/Users/alice'), '~/.local/bin/uv')
        self.assertEqual(hp.redact_path('/home/bob/bin/git', home='/nowhere'), '~/bin/git')
        self.assertEqual(hp.redact_path(r'C:\Users\Carol\AppData\py.exe', home='/nowhere'), r'~\AppData\py.exe')
        self.assertEqual(hp.redact_path('/usr/bin/git', home='/Users/alice'), '/usr/bin/git')
        self.assertIsNone(hp.redact_path(None))

    def test_scrub_is_recursive_case_insensitive_and_covers_keys(self):
        dirty = {'a': ['x ALICE y', {'alice-key': 'host-mbp.local'}], 'n': 3}
        self.assertEqual(hp.scrub(dirty, ['alice', 'host-mbp']),
                         {'a': ['x [redacted] y', {'[redacted]-key': '[redacted].local'}], 'n': 3})

    def test_needles_skip_strings_too_short_to_redact_safely(self):
        with mock.patch.object(hp.getpass, 'getuser', return_value='al'):
            self.assertNotIn('al', hp.sensitive_needles())

    def test_report_is_scrubbed_end_to_end(self):
        def leaky_base():
            return {'os': {'version': 'built by skyler-test-user on HOST-ZZ9'}}

        with mock.patch.object(hp, 'probe_base', leaky_base), \
                mock.patch.object(hp, 'sensitive_needles', return_value=['skyler-test-user', 'host-zz9']):
            dumped = json.dumps(hp.build_report())
        self.assertNotIn('skyler-test-user', dumped)
        self.assertNotIn('HOST-ZZ9', dumped)


class BaseSectionTests(unittest.TestCase):
    def test_default_run_executes_no_external_command(self):
        boom = AssertionError('the default probe must not spawn a process')
        with mock.patch.object(hp.subprocess, 'run', side_effect=boom), \
                mock.patch.object(hp.subprocess, 'Popen', side_effect=boom):
            report = hp.build_report()
        self.assertEqual(report['sections_run'], ['base'])
        for absent in ('nvidia', 'wsl', 'macos'):
            self.assertNotIn(absent, report)
        json.dumps(report)  # serialisable

    def test_os_facts_come_from_sys_and_os_on_windows_too(self):
        win = mock.Mock(major=10, minor=0, build=26100)
        with mock.patch.object(hp.sys, 'platform', 'win32'), \
                mock.patch.object(hp.sys, 'getwindowsversion', return_value=win, create=True), \
                mock.patch.dict(hp.os.environ, {'PROCESSOR_ARCHITECTURE': 'AMD64'}):
            self.assertEqual(hp.system_name(), 'Windows')
            self.assertEqual(hp.os_facts(), {'release': '10.0', 'version': 'build 26100', 'machine': 'AMD64'})

    def test_base_shape_on_this_platform(self):
        report = hp.build_report()
        self.assertEqual(report['schema_version'], hp.SCHEMA_VERSION)
        self.assertTrue(report['os']['system'])
        self.assertTrue(report['architecture']['machine'])
        self.assertGreater(report['cpu']['logical_cores'], 0)
        self.assertGreater(report['ram']['total_bytes'], 2**28)  # every CI runner has > 256 MiB
        self.assertEqual(set(report['executables']), set(hp.EXECUTABLES))

    def test_no_hostname_username_or_environment_in_report(self):
        dumped = json.dumps(hp.build_report()).lower()
        for needle in hp.sensitive_needles():
            self.assertNotIn(needle.lower(), dumped)
        for key in ('environ', 'hostname', 'serial', 'uuid', 'username'):
            self.assertNotIn(key, dumped)

    def test_executables_report_missing_tools_gracefully(self):
        found = hp.probe_executables(('git', 'ghost'), which=lambda n: '/home/zed/bin/git' if n == 'git' else None)
        self.assertEqual(found, {'git': {'found': True, 'path': '~/bin/git'},
                                 'ghost': {'found': False, 'path': None}})

    def test_ram_failure_is_none_not_an_exception(self):
        with mock.patch.object(hp.sys, 'platform', 'linux'), \
                mock.patch.object(hp.os, 'sysconf', side_effect=ValueError, create=True):
            self.assertIsNone(hp.total_ram_bytes())

    def test_inside_wsl_detection(self):
        self.assertFalse(hp.inside_wsl('/definitely/not/here'))


class NvidiaTests(unittest.TestCase):
    CSV = 'NVIDIA GeForce RTX 5070, 12227, 581.29, 12.0\n'
    BANNER = ('| NVIDIA-SMI 581.29   Driver Version: 581.29   CUDA Version: 13.0 |\n'
              '|  0  N/A  1234  C  ...\\secret-project\\train.exe  900MiB |\n')

    def test_parses_gpu_and_cuda_version_without_keeping_the_banner(self):
        run = fake_run([(('nvidia-smi', '--query-gpu=name,memory.total,driver_version,compute_cap'),
                         {'status': 'ok', 'stdout': self.CSV}),
                        (('nvidia-smi',), {'status': 'ok', 'stdout': self.BANNER})])
        section = hp.probe_nvidia(run=run)
        self.assertEqual(section, {'status': 'ok', 'cuda_version': '13.0', 'gpus': [{
            'name': 'NVIDIA GeForce RTX 5070', 'memory_total_mib': 12227,
            'driver_version': '581.29', 'compute_cap': '12.0'}]})
        self.assertNotIn('secret-project', json.dumps(section))

    def test_query_never_asks_for_identifying_fields(self):
        run = fake_run([])
        hp.probe_nvidia(run=run)
        asked = ' '.join(' '.join(c) for c in run.calls).lower()
        for forbidden in ('serial', 'uuid', 'pci.bus_id'):
            self.assertNotIn(forbidden, asked)

    def test_old_driver_without_compute_cap_falls_back(self):
        run = fake_run([(('nvidia-smi', '--query-gpu=name,memory.total,driver_version,compute_cap'),
                         {'status': 'failed', 'returncode': 2}),
                        (('nvidia-smi', '--query-gpu=name,memory.total,driver_version'),
                         {'status': 'ok', 'stdout': 'Tesla K80, 11441, 470.82\n'})])
        self.assertEqual(hp.probe_nvidia(run=run)['gpus'],
                         [{'name': 'Tesla K80', 'memory_total_mib': 11441, 'driver_version': '470.82'}])

    def test_missing_and_timeout_are_graceful(self):
        self.assertEqual(hp.probe_nvidia(run=fake_run([])), {'status': 'missing'})
        self.assertEqual(hp.probe_nvidia(run=fake_run([(('nvidia-smi',), {'status': 'timeout'})])),
                         {'status': 'timeout'})


class WslTests(unittest.TestCase):
    LISTING = '  NAME                   STATE           VERSION\n* Ubuntu-24.04           Running         2\n  docker-desktop         Stopped         2\n'

    def test_not_applicable_off_windows(self):
        with mock.patch.object(hp, 'system_name', return_value='Linux'):
            run = fake_run([])
            self.assertEqual(hp.probe_wsl(run=run)['status'], 'not_applicable')
            self.assertEqual(run.calls, [])

    def test_parses_distros_and_default_version_on_windows(self):
        run = fake_run([(('wsl', '--list'), {'status': 'ok', 'stdout': self.LISTING}),
                        (('wsl', '--status'), {'status': 'ok', 'stdout': 'Default Distribution: Ubuntu-24.04\nDefault Version: 2\n'})])
        with mock.patch.object(hp, 'system_name', return_value='Windows'):
            section = hp.probe_wsl(run=run)
        self.assertEqual(section['distros'], [
            {'name': 'Ubuntu-24.04', 'state': 'Running', 'wsl_version': 2, 'default': True},
            {'name': 'docker-desktop', 'state': 'Stopped', 'wsl_version': 2, 'default': False}])
        self.assertEqual(section['default_wsl_version'], 2)

    def test_wsl_not_installed_on_windows(self):
        with mock.patch.object(hp, 'system_name', return_value='Windows'):
            section = hp.probe_wsl(run=fake_run([]))
        self.assertEqual((section['status'], section['default_wsl_version']), ('missing', None))


class MacosTests(unittest.TestCase):
    HARDWARE = {'SPHardwareDataType': [{
        'chip_type': 'Apple M4 Pro', 'machine_model': 'Mac16,8', 'machine_name': 'MacBook Pro',
        'number_processors': 'proc 12:8:4', 'physical_memory': '24 GB',
        'serial_number': 'SERIAL-SHOULD-NOT-LEAK', 'platform_UUID': 'UUID-SHOULD-NOT-LEAK',
        'provisioning_UDID': 'UDID-SHOULD-NOT-LEAK'}]}
    DISPLAYS = {'SPDisplaysDataType': [{
        'sppci_model': 'Apple M4 Pro', 'sppci_cores': '16', 'sppci_device_type': 'spdisplays_gpu',
        'spdisplays_mtlgpufamilysupport': 'spdisplays_metal4',
        'spdisplays_ndrvs': [{'_spdisplays_display-serial-number': 'DISPLAY-SERIAL-SHOULD-NOT-LEAK'}]}]}
    THUNDERBOLT = {'SPThunderboltDataType': [{
        'domain_uuid_key': 'TB-UUID-SHOULD-NOT-LEAK', 'switch_uid_key': 'TB-UID-SHOULD-NOT-LEAK',
        'receptacle_1_tag': {'current_speed_key': 'Up to 120 Gb/s'}}]}

    def run_all(self):
        return fake_run([
            (('system_profiler', 'SPHardwareDataType'), {'status': 'ok', 'stdout': json.dumps(self.HARDWARE)}),
            (('system_profiler', 'SPDisplaysDataType'), {'status': 'ok', 'stdout': json.dumps(self.DISPLAYS)}),
            (('system_profiler', 'SPThunderboltDataType'), {'status': 'ok', 'stdout': json.dumps(self.THUNDERBOLT)}),
            (('sysctl', '-n', 'hw.memsize'), {'status': 'ok', 'stdout': '25769803776\n'}),
            (('sysctl', '-n', 'machdep.cpu.brand_string'), {'status': 'ok', 'stdout': 'Apple M4 Pro\n'})])

    def test_allowlist_keeps_hardware_facts_and_drops_every_identifier(self):
        with mock.patch.object(hp, 'system_name', return_value='Darwin'):
            section = hp.probe_macos(run=self.run_all())
        self.assertEqual(section['hardware']['chip_type'], 'Apple M4 Pro')
        self.assertEqual(section['gpus'][0]['sppci_cores'], '16')
        self.assertEqual(section['thunderbolt_link_speeds'], ['Up to 120 Gb/s'])
        self.assertEqual(section['sysctl']['hw.memsize'], 25769803776)
        self.assertIsNone(section['sysctl']['iogpu.wired_limit_mb'])  # tool answered 'missing'
        self.assertNotIn('SHOULD-NOT-LEAK', json.dumps(section))

    def test_not_applicable_off_macos(self):
        with mock.patch.object(hp, 'system_name', return_value='Windows'):
            run = fake_run([])
            self.assertEqual(hp.probe_macos(run=run), {'status': 'not_applicable'})
            self.assertEqual(run.calls, [])

    def test_missing_tools_and_garbage_output_are_partial_not_fatal(self):
        run = fake_run([(('system_profiler', 'SPHardwareDataType'), {'status': 'ok', 'stdout': 'not json'}),
                        (('system_profiler', 'SPDisplaysDataType'), {'status': 'timeout'})])
        with mock.patch.object(hp, 'system_name', return_value='Darwin'):
            section = hp.probe_macos(run=run)
        self.assertEqual(section['status'], 'partial')
        self.assertEqual(section['commands'], {'SPHardwareDataType': 'unparseable',
                                               'SPDisplaysDataType': 'timeout',
                                               'SPThunderboltDataType': 'missing'})
        self.assertIsNone(section['hardware'])
        self.assertEqual(section['gpus'], [])


class CliTests(unittest.TestCase):
    def test_default_cli_prints_one_json_document(self):
        out = io.StringIO()
        self.assertEqual(hp.main([], out=out), 0)
        self.assertEqual(json.loads(out.getvalue())['sections_run'], ['base'])

    def test_all_flag_opts_into_every_section_and_survives_missing_tools(self):
        out = io.StringIO()
        with mock.patch.object(hp, 'run_bounded', fake_run([])), \
                mock.patch.object(hp.build_report, '__defaults__', (False, False, False, hp.DEFAULT_TIMEOUT_S, fake_run([]))):
            hp.main(['--all', '--pretty'], out=out)
        report = json.loads(out.getvalue())
        self.assertEqual(report['sections_run'], ['base', 'nvidia', 'wsl', 'macos'])
        self.assertEqual(report['nvidia'], {'status': 'missing'})

    def test_timeout_bounds_are_enforced(self):
        for bad in ('0', '-1', '61'):
            with self.assertRaises(SystemExit), mock.patch.object(sys, 'stderr', io.StringIO()):
                hp.parse_args(['--timeout', bad])
        self.assertEqual(hp.parse_args(['--timeout', '2.5']).timeout, 2.5)


if __name__ == '__main__':
    unittest.main()
