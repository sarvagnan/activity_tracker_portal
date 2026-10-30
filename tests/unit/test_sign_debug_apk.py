"""Exercise the actual signing shell script without Java or an Android SDK."""

from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[2] / 'sender/scripts/sign-debug-apk.sh'


class SignDebugApkTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root / 'checkout with spaces'
        self.scripts = self.repo / 'sender/scripts'
        self.scripts.mkdir(parents=True)
        self.script = self.scripts / SCRIPT.name
        shutil.copyfile(SCRIPT, self.script)
        self.script.chmod(0o755)
        self.input = self.root / 'unsigned input.apk'
        self.input.write_bytes(b'unsigned')
        self.output = self.root / 'signed output.apk'
        self.key = self.root / 'keys/development.keystore'
        self.log = self.root / 'calls.jsonl'
        self.jdk = self.root / 'jdk'
        self.sdk = self.root / 'sdk'
        self.env = {k: v for k, v in os.environ.items() if k not in (
            'ACTIVITY_TRACKER_DEBUG_KEYSTORE', 'ACTIVITY_TRACKER_SIGNING_JAVA_HOME',
            'ANDROID_HOME', 'ANDROID_SDK_ROOT', 'ANDROID_BUILD_TOOLS_VERSION',
        )}
        self.env.update(HOME=str(self.root / 'home'), ANDROID_HOME=str(self.sdk),
                        ACTIVITY_TRACKER_DEBUG_KEYSTORE=str(self.key),
                        STUB_LOG=str(self.log), STUB_JDK=str(self.jdk))
        self.executable(self.scripts / 'jdk.sh', '#!/bin/sh\nprintf "%s\\n" "$STUB_JDK"\n')
        self.executable(self.jdk / 'bin/keytool', f'''#!{sys.executable}
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write(json.dumps(['keytool', *args]) + '\\n')
pathlib.Path(args[args.index('-keystore') + 1]).write_bytes(b'development key')
''')
        self.signer = f'''#!{sys.executable}
import json, os, pathlib, shutil, sys
args = sys.argv[1:]
with open(os.environ['STUB_LOG'], 'a') as log:
    log.write(json.dumps([str(pathlib.Path(sys.argv[0]).parent.name), *args]) + '\\n')
if args[0] == 'sign':
    shutil.copyfile(args[-1], args[args.index('--out') + 1])
else:
    sys.exit(int(os.environ.get('STUB_VERIFY_EXIT', '0')))
'''
        self.executable(self.sdk / 'build-tools/36.0.0/apksigner', self.signer)

    def executable(self, path, contents):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        path.chmod(0o755)

    def run_script(self, *args, shell=None):
        command = [str(self.script), *map(str, args or (self.input, self.output))]
        if shell:
            command.insert(0, shell)
        return subprocess.run(command, env=self.env, cwd=self.root, text=True,
                              capture_output=True, timeout=10)

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_key_created_once_reused_and_result_verified_under_sh_and_bash(self):
        for shell in ('/bin/sh', '/bin/bash'):
            result = self.run_script(shell=shell)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Verified signed development APK', result.stdout)
        self.assertEqual(self.output.read_bytes(), self.input.read_bytes())
        self.assertEqual(sum(call[0] == 'keytool' for call in self.calls()), 1)
        self.assertEqual(sum(call[1] == 'verify' for call in self.calls()), 2)
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)

    def test_refuses_in_repo_key_including_missing_parents(self):
        self.env['ACTIVITY_TRACKER_DEBUG_KEYSTORE'] = str(self.repo / 'new/keys/debug.keystore')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('outside the repository checkout', result.stderr)
        self.assertFalse((self.repo / 'new').exists())
        self.assertFalse(self.log.exists())

    def test_refuses_symlink_into_repo(self):
        link = self.root / 'linked-checkout'
        link.symlink_to(self.repo, target_is_directory=True)
        self.env['ACTIVITY_TRACKER_DEBUG_KEYSTORE'] = str(link / 'debug.keystore')
        self.assertIn('outside the repository checkout', self.run_script().stderr)

    def test_missing_input_requests_build_before_tools_are_used(self):
        self.input.unlink()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('npm run build:android --prefix sender first', result.stderr)
        self.assertFalse(self.log.exists())

    def test_verification_failure_propagated(self):
        self.env['STUB_VERIFY_EXIT'] = '42'
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('signed APK verification failed', result.stderr)
        self.assertNotIn('Verified signed development APK', result.stdout)

    def test_missing_sdk_and_missing_apksigner(self):
        for sdk in (self.root / 'missing-sdk', self.root / 'empty-sdk'):
            if sdk.name == 'empty-sdk':
                sdk.mkdir()
            self.env['ANDROID_HOME'] = str(sdk)
            result = self.run_script()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('SDK missing' if sdk.name == 'missing-sdk' else 'apksigner missing', result.stderr)
            self.assertFalse(self.key.exists())

    def test_numeric_stable_selection_and_explicit_version(self):
        for version in ('9.0.0', '36.1.0', '37.0.0-rc1'):
            self.executable(self.sdk / f'build-tools/{version}/apksigner', self.signer)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('build-tools 36.1.0', result.stdout)
        self.env['ANDROID_BUILD_TOOLS_VERSION'] = '36.0.0'
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('build-tools 36.0.0', result.stdout)

    def test_generated_gradle_version_preferred_over_newer_installed_tools(self):
        properties = self.repo / 'sender/app/android/gradle.properties'
        properties.parent.mkdir(parents=True)
        properties.write_text('android.buildToolsVersion=36.0.0\n')
        self.executable(self.sdk / 'build-tools/36.1.0/apksigner', self.signer)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('build-tools 36.0.0', result.stdout)
        self.env['ANDROID_BUILD_TOOLS_VERSION'] = '36.1.0'
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('build-tools 36.1.0', result.stdout)

    def test_default_paths_and_sdk_root_fallback(self):
        del self.env['ACTIVITY_TRACKER_DEBUG_KEYSTORE']
        self.env['ANDROID_SDK_ROOT'] = self.env.pop('ANDROID_HOME')
        default_dir = self.repo / 'sender/app/android/app/build/outputs/apk/debug'
        default_dir.mkdir(parents=True)
        (default_dir / 'app-debug-unsigned.apk').write_bytes(b'default unsigned')
        result = subprocess.run([str(self.script)], env=self.env, cwd=self.root,
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((default_dir / 'app-debug.apk').read_bytes(), b'default unsigned')
        self.assertTrue((Path(self.env['HOME']) / '.config/activity-tracker/android-debug.keystore').is_file())

    def test_same_input_and_output_refused(self):
        result = self.run_script(self.input, self.input)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('paths must differ', result.stderr)
        self.assertEqual(self.input.read_bytes(), b'unsigned')
