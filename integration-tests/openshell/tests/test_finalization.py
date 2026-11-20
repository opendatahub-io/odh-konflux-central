"""Exercise the final task boundary, including lost publication responses."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

from test_version_tests import images, manifest, m


class Finalization(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.args = SimpleNamespace(data=root / 'data', reports=root / 'reports', artifacts=root / 'artifacts')
        self.args.data.mkdir()
        (self.args.reports / 'smoke').mkdir(parents=True)
        m.write(self.args.data / 'manifest.json', dict(manifest(), images=images()))
        m.write(self.args.reports / 'agent-sandbox-installation.json', dict(m.AGENT_SANDBOX, ready=True))
        (self.args.reports / 'smoke/e2e-odh-smoke-xunit.xml').write_text('<testsuite><testcase/></testsuite>')
        (self.args.reports / 'smoke.exit').write_text('0')
        self.env = {'PIPELINE_UID': 'our-run', 'PIPELINE_STATUS': 'Succeeded',
                    'ARTIFACT_EXIT_CODE_FILE': str(root / 'upload.exit'),
                    'ARTIFACT_REFERENCE': 'quay.io/opendatahub/odh-ci-artifacts:openshell-version-1',
                    'TEST_OUTPUT_RESULT': str(root / 'TEST_OUTPUT')}
        (root / 'upload.exit').write_text('0')
        self.receipt = {'immutable': True, 'data': {'receipt.json': json.dumps({'owner_uid': 'our-run', 'requested': True})}}

    def finalize(self):
        with patch.dict(m.os.environ, self.env, clear=True), patch.object(m, 'cluster_request', return_value=self.receipt):
            return m.finalize(self.args)

    def test_restart_reuses_the_first_frozen_record_and_successful_output(self):
        first = self.finalize()
        self.assertEqual(first['status'], 'passed')
        with patch.dict(m.os.environ, self.env, clear=True):
            sealed = m.seal(self.args)
            self.assertEqual(json.loads(Path(self.env['TEST_OUTPUT_RESULT']).read_text())['result'], 'SUCCESS')
            Path(self.env['ARTIFACT_EXIT_CODE_FILE']).write_text('1')
            self.assertEqual(m.seal(self.args), sealed)
        with patch.object(m, 'now', return_value='a later timestamp'), patch.object(m, 'cluster_request') as api:
            self.assertEqual(m.finalize(self.args), first)
            api.assert_not_called()

    def test_failed_missing_and_foreign_cleanup_receipts_never_pass(self):
        self.receipt['data']['receipt.json'] = json.dumps({'owner_uid': 'our-run', 'requested': False})
        self.assertEqual(self.finalize()['status'], 'error')
        (self.args.data / 'test-result.json').unlink()
        self.env['CLEANUP_WAIT_SECONDS'] = '0'
        self.assertEqual(self.finalize()['status'], 'error')
        (self.args.data / 'test-result.json').unlink()
        self.env.pop('CLEANUP_WAIT_SECONDS')
        self.receipt['data']['receipt.json'] = json.dumps({'owner_uid': 'another-run', 'requested': True})
        with self.assertRaisesRegex(ValueError, 'another run'):
            self.finalize()

    def test_missing_receipt_is_polled_before_freezing(self):
        missing = urllib.error.HTTPError('cm', 404, 'missing', {}, None)
        with patch.dict(m.os.environ, self.env, clear=True), patch.object(m, 'cluster_request', side_effect=[missing, self.receipt]), patch.object(m.time, 'sleep'):
            self.assertEqual(m.finalize(self.args)['status'], 'passed')

    def test_oci_failure_changes_result_to_error_and_standard_error_output(self):
        self.finalize()
        Path(self.env['ARTIFACT_EXIT_CODE_FILE']).write_text('1')
        with patch.dict(m.os.environ, self.env, clear=True):
            sealed = m.seal(self.args)
        self.assertEqual(sealed['status'], 'error')
        self.assertEqual(sealed['artifacts']['status'], 'error')
        self.assertEqual(json.loads(Path(self.env['TEST_OUTPUT_RESULT']).read_text())['result'], 'ERROR')

    def test_publisher_retry_after_lost_success_response_uses_identical_record(self):
        self.finalize()
        with patch.dict(m.os.environ, self.env, clear=True):
            sealed = m.seal(self.args)
        committed = []
        def publish(record, token):
            committed.append(copy.deepcopy(record))
            if len(committed) == 1:
                raise TimeoutError('Git commit succeeded but response was lost')
        with patch.object(m, 'publish_record', side_effect=publish), patch.object(m.time, 'sleep'):
            m.publish_frozen(self.args, token='test-placeholder')
            m.publish_frozen(self.args, token='test-placeholder')
        self.assertEqual(committed, [sealed, sealed, sealed])

    def test_artifact_staging_excludes_credentials_and_symlinks(self):
        self.finalize()
        (self.args.reports / 'kubeconfig.json').write_text('private')
        (self.args.reports / 'copy.json').symlink_to(self.args.reports / 'kubeconfig.json')
        m.stage_artifacts(self.args)
        self.assertTrue((self.args.artifacts / 'smoke/e2e-odh-smoke-xunit.xml').is_file())
        self.assertTrue((self.args.artifacts / 'test-result.json').is_file())
        self.assertFalse((self.args.artifacts / 'kubeconfig.json').exists())
        self.assertFalse((self.args.artifacts / 'copy.json').exists())


class IndependentCleanup(unittest.TestCase):
    def test_actual_cleanup_script_deletes_only_owned_claim_with_uid_precondition(self):
        import yaml
        pipeline = yaml.safe_load((Path(__file__).parents[1] / 'version-tests.yaml').read_text())
        script = next(task for task in pipeline['spec']['finally'] if task['name'] == 'cleanup-cluster')['taskSpec']['steps'][0]['script']
        namespace = {}
        # Run the actual body with an injected API, avoiding service-account bootstrap.
        body = script[script.index("uid = os.environ['PIPELINE_UID']"):]
        own = {'metadata': {'name': 'own', 'uid': 'claim-uid', 'ownerReferences': [{'kind': 'PipelineRun', 'uid': 'our-run'}]}}
        other = {'metadata': {'name': 'other', 'uid': 'other-uid', 'ownerReferences': [{'kind': 'PipelineRun', 'uid': 'another-run'}]}}
        calls = []
        def api(path, method='GET', value=None):
            calls.append((path, method, value))
            return {'items': [own, other]} if method == 'GET' else {}
        import datetime
        import os
        import urllib.parse
        namespace.update(api=api, namespace='tenant', os=os, json=json, datetime=datetime, urllib=urllib)
        with patch.dict(os.environ, {'PIPELINE_UID': 'our-run'}):
            exec(compile(body, '<cleanup>', 'exec'), namespace)
        deletions = [(path, value) for path, method, value in calls if method == 'DELETE']
        self.assertEqual(len(deletions), 1)
        self.assertTrue(deletions[0][0].endswith('/own'))
        self.assertEqual(deletions[0][1]['preconditions'], {'uid': 'claim-uid'})
        receipt = json.loads(calls[-1][2]['data']['receipt.json'])
        self.assertTrue(receipt['requested'])
