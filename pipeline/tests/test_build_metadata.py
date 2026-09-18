"""Exercise the pipeline's metadata publisher against a local Git remote.

Run with: python3 -m unittest discover -s pipeline/tests -v
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import textwrap
import unittest


PIPELINE = Path(__file__).resolve().parents[1] / "multi-arch-operator-build.yaml"
REAL_GIT = shutil.which("git")


class BuildMetadataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=str(self.root / "gitconfig"),
                        GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        self.git("config", "--global", "user.name", "Test")
        self.git("config", "--global", "user.email", "test@example.com")
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.git("init", "--bare", "--initial-branch=main", str(self.remote))
        self.git("clone", str(self.remote), str(self.seed))
        self.write(self.seed / "components/odh-operator/docs/README.md", "metadata\n")
        self.commit(self.seed, "Initial metadata")
        self.git("push", "origin", "main", cwd=self.seed)
        self.work = self.root / "work"
        self.write(self.work / "cachi2/prefetched-manifests/component/resource.yaml", "kind: Deployment\n")
        self.write(self.work / "source/build/operands-map.yaml", "operands: {}\n")
        self.write(self.work / "source/build/manifests-config.yaml", "manifests: {}\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Inject a real concurrent commit immediately before the first push.
        # Failure modes also exercise explicit recovery errors and retry limits.
        wrapper = '''#!/usr/bin/env python3
import os
from pathlib import Path
import subprocess
import sys
args = sys.argv[1:]
real = os.environ['REAL_GIT']
mode = os.environ.get('TEST_MODE', '')
marker = Path(os.environ['PUSH_MARKER'])
if args[0] == 'push':
    first = not marker.exists()
    with marker.open('a') as stream:
        stream.write('push\\n')
    if first and mode in ('race', 'same', 'conflict'):
        seed = Path(os.environ['SEED'])
        digest = 'other' if mode == 'race' else 'test-digest'
        target = seed / 'components/odh-operator' / digest
        if mode == 'same':
            import shutil
            shutil.copytree(Path.cwd() / 'components/odh-operator/test-digest', target)
        else:
            target = target / 'manifests/component/resource.yaml'
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('concurrent update\\n')
        for command in (['add', '.'], ['commit', '-m', 'Concurrent build'], ['push', 'origin', 'main']):
            subprocess.run([real] + command, cwd=seed, check=True)
    if mode in ('exhaust', 'fetch-failure'):
        sys.exit(1)
if args[0] == 'fetch' and marker.exists() and mode == 'fetch-failure':
    sys.exit(1)
sys.exit(subprocess.call([real] + args))
'''
        self.write(self.bin / "git", wrapper)
        (self.bin / "git").chmod(0o755)
        self.write(self.bin / "sleep", "#!/bin/sh\nexit 0\n")
        (self.bin / "sleep").chmod(0o755)
        self.env.update(PATH=str(self.bin) + os.pathsep + self.env["PATH"],
                        REAL_GIT=REAL_GIT, SEED=str(self.seed),
                        PUSH_MARKER=str(self.root / "pushes"),
                        SOURCE_ARTIFACT="test", CACHI2_ARTIFACT="test",
                        OUTPUT_IMAGE_DIGEST="sha256:test-digest", GITHUB_TOKEN="test",
                        BUILD_METADATA_REPO="test")
        task = PIPELINE.read_text().split("  - name: push-build-metadata\n", 1)[1]
        script = task.split("          script: |\n", 1)[1].split("\n    when:", 1)[0]
        script = textwrap.dedent(script).replace("/var/workdir", str(self.work))
        script = script.replace(
            '"https://x-access-token:${GITHUB_TOKEN}@github.com/${BUILD_METADATA_REPO}.git"',
            '"' + str(self.remote) + '"')
        self.script = self.root / "publish.sh"
        self.write(self.script, script)

    @staticmethod
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def git(self, *args, cwd=None):
        return subprocess.check_output([REAL_GIT, *args], cwd=cwd, env=self.env,
                                       stderr=subprocess.STDOUT, text=True).strip()

    def commit(self, cwd, message):
        self.git("add", ".", cwd=cwd)
        self.git("commit", "-m", message, cwd=cwd)

    def publish(self, mode="", success=True):
        self.env["TEST_MODE"] = mode
        result = subprocess.run(["bash", str(self.script)], env=self.env,
                                capture_output=True, text=True, timeout=30)
        output = result.stdout + result.stderr
        if success:
            self.assertEqual(result.returncode, 0, output)
        else:
            self.assertNotEqual(result.returncode, 0, output)
        return output

    def remote_file(self, path):
        return self.git("show", "main:components/odh-operator/" + path, cwd=self.remote)

    def test_publish_and_identical_rerun(self):
        self.publish()
        before = self.git("rev-parse", "main", cwd=self.remote)
        shutil.rmtree(self.work / "odh-build-metadata")
        self.assertIn("Metadata already published", self.publish())
        self.assertEqual(before, self.git("rev-parse", "main", cwd=self.remote))
        self.assertEqual("kind: Deployment", self.remote_file("test-digest/manifests/component/resource.yaml"))

    def test_concurrent_different_digest(self):
        self.assertIn("Push succeeded (attempt 2)", self.publish("race"))
        self.assertEqual("concurrent update", self.remote_file("other/manifests/component/resource.yaml"))
        self.assertEqual("kind: Deployment", self.remote_file("test-digest/manifests/component/resource.yaml"))
        self.assertEqual("3", self.git("rev-list", "--count", "main", cwd=self.remote))

    def test_concurrent_identical_digest(self):
        self.assertIn("Push succeeded (attempt 2)", self.publish("same"))
        self.assertEqual("2", self.git("rev-list", "--count", "main", cwd=self.remote))

    def test_conflict_fails_without_overwriting(self):
        self.assertIn("ERROR: Could not rebase", self.publish("conflict", success=False))
        self.assertEqual("concurrent update", self.remote_file("test-digest/manifests/component/resource.yaml"))
        self.assertEqual("push\n", (self.root / "pushes").read_text())

    def test_fetch_failure(self):
        self.assertIn("ERROR: Could not fetch", self.publish("fetch-failure", success=False))
        self.assertEqual("push\n", (self.root / "pushes").read_text())

    def test_retry_exhaustion(self):
        self.assertIn("ERROR: Push failed after 10 attempts", self.publish("exhaust", success=False))
        self.assertEqual(10, len((self.root / "pushes").read_text().splitlines()))

    def test_optional_charts(self):
        self.write(self.work / "cachi2/prefetched-charts/component/Chart.yaml", "name: component\n")
        self.write(self.work / "source/build/charts-config.yaml", "charts: {}\n")
        self.publish()
        self.assertEqual("name: component", self.remote_file("test-digest/charts/component/Chart.yaml"))
        self.assertEqual("charts: {}", self.remote_file("test-digest/charts-config.yaml"))


if __name__ == "__main__":
    unittest.main()
