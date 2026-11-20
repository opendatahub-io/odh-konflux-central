import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

sys.path.insert(0, str(Path(__file__).parents[1]))
import coordinator as c
import version_tests as v
from test_version_tests import REV, DIGEST, TAG, images


def snapshot(role, branch=None):
    component = 'odh-openshell-' + role + '-ci'
    return {'metadata': {'name': role + '-snapshot', 'uid': role + '-uid',
        'creationTimestamp': '2026-11-20T10:00:00Z', 'labels': {
            'pac.test.appstudio.openshift.io/event-type': 'push',
            'appstudio.openshift.io/component': component,
            'appstudio.openshift.io/build-pipelinerun': role + '-build'},
        'annotations': {'pac.test.appstudio.openshift.io/branch': branch or ('main' if role == 'e2e' else 'refs/tags/' + TAG)}},
        'spec': {'application': 'opendatahub-builds', 'components': [{'name': component,
            'containerImage': images()[role]['image'], 'source': {'git': {'url': v.SOURCE, 'revision': REV}}}]}}


def records():
    return [c.build_record(snapshot(role)) for role in v.ROLES]


def group():
    return c.groups(records(), ['smoke'])[0]


class FakeAPI:
    def __init__(self):
        self.items = {}
        self.sequence = 0
        self.created_runs = 0
        self.lose_dispatch_response = False
        self.snapshots = []
        self.lose_marker_response = False

    def request(self, path, query=None, data=None, method='GET', **kwargs):
        if method == 'GET':
            if path == c.SNAPSHOTS:
                return {'items': copy.deepcopy(self.snapshots)}
            if path == c.CONFIGMAPS:
                return {'items': [copy.deepcopy(item) for item in self.items.values()
                    if item['kind'] == 'ConfigMap' and item['metadata']['labels'][c.PREFIX + 'kind'] == 'build']}
            if path not in self.items:
                raise urllib.error.HTTPError(path, 404, 'not found', {}, None)
            return copy.deepcopy(self.items[path])
        if method == 'POST':
            key = path + '/' + data['metadata']['name']
            if key in self.items:
                raise urllib.error.HTTPError(path, 409, 'exists', {}, None)
            self.sequence += 1
            item = copy.deepcopy(data)
            item['metadata'].update(uid='uid-' + str(self.sequence), resourceVersion=str(self.sequence))
            self.items[key] = item
            if data['kind'] == 'PipelineRun':
                self.created_runs += 1
                if self.lose_dispatch_response:
                    self.lose_dispatch_response = False
                    raise TimeoutError('the create committed but the response was lost')
            return copy.deepcopy(item)
        if method == 'PATCH':
            item = self.items[path]
            if data.get('metadata', {}).get('resourceVersion') != item['metadata']['resourceVersion']:
                raise urllib.error.HTTPError(path, 409, 'conflict', {}, None)
            if item.get('immutable') and 'data' in data:
                raise AssertionError('cannot patch immutable group data')
            metadata = copy.deepcopy(data.get('metadata', {}))
            annotations = metadata.pop('annotations', {})
            item['metadata'].update(metadata)
            if annotations:
                item['metadata'].setdefault('annotations', {}).update(annotations)
            self.sequence += 1
            item['metadata']['resourceVersion'] = str(self.sequence)
            if annotations and self.lose_marker_response:
                self.lose_marker_response = False
                raise TimeoutError('the group annotation committed but the response was lost')
            return copy.deepcopy(item)
        raise AssertionError((method, path))


class Coordination(unittest.TestCase):
    def test_all_completion_orders_form_the_same_exact_group(self):
        import itertools
        for sequence in itertools.permutations(records()):
            self.assertEqual(c.groups(list(sequence), ['smoke'])[0]['group_id'], group()['group_id'])

    def test_late_component_or_runner_is_picked_up_without_a_gateway_rebuild(self):
        retained = records()[:2]
        self.assertEqual(c.groups(retained, ['smoke']), [])
        retained.append(records()[2])
        self.assertEqual(c.groups(retained, ['smoke']), [])
        delayed = records()[3]
        delayed['completed_at'] = '2026-11-22T10:00:00Z'
        retained.append(delayed)
        self.assertEqual(c.groups(retained, ['smoke'])[0]['source_revision'], REV)

    def test_same_commit_rule_includes_historical_runner_and_every_runtime(self):
        for i in range(4):
            altered = records()
            altered[i]['source_revision'] = 'c' * 40
            self.assertEqual(c.groups(altered, ['smoke']), [])

    def test_pr_branch_fork_or_missing_successful_build_provenance_cannot_record(self):
        self.assertIsNone(c.build_record(snapshot('gateway', 'main')))
        self.assertIsNone(c.build_record(snapshot('e2e', 'feature')))
        pr = snapshot('gateway')
        pr['metadata']['labels']['pac.test.appstudio.openshift.io/pull-request'] = '1'
        self.assertIsNone(c.build_record(pr))
        for change in ('source', 'build', 'digest'):
            item = snapshot('gateway')
            if change == 'source':
                item['spec']['components'][0]['source']['git']['url'] = 'https://github.com/untrusted/fork'
            elif change == 'build':
                del item['metadata']['labels']['appstudio.openshift.io/build-pipelinerun']
            else:
                item['spec']['components'][0]['containerImage'] = 'quay.io/untrusted/image@' + DIGEST
            with self.subTest(change=change), self.assertRaises(ValueError):
                c.build_record(item)

    def test_worker_rejects_changed_images_in_the_retained_group(self):
        changed = group()
        changed['images']['supervisor']['image'] = changed['images']['supervisor']['image'].replace('b' * 64, 'c' * 64)
        with self.assertRaisesRegex(ValueError, 'identity'):
            v.validate_group(changed)

    def test_odh_and_full_selections_are_rejected(self):
        for tier in ('odh', 'full'):
            value = group()
            value['tiers'] = [tier]
            with self.assertRaises(ValueError):
                v.validate_group(value)

    def test_one_reconciler_records_and_keeps_evidence_after_snapshot_pruning(self):
        api = FakeAPI()
        api.snapshots = [snapshot(role) for role in v.ROLES]
        with patch.object(v, 'cluster_request', side_effect=api.request):
            self.assertEqual(len(c.collect_records()), 4)
            self.assertEqual(len(c.collect_records()), 4)
            api.snapshots = []
            retained = c.collect_records()
        self.assertEqual(len(c.groups(retained, ['smoke'])), 1)
        self.assertEqual(len(api.items), 4)
        self.assertTrue(all(cm['immutable'] and not cm['metadata'].get('ownerReferences') for cm in api.items.values()))

    def test_reconcile_launches_after_late_build_and_keeps_deduplication_after_pruning(self):
        api = FakeAPI()
        api.snapshots = [snapshot('gateway')]
        with patch.object(v, 'cluster_request', side_effect=api.request), patch.object(c, 'registry_ready', return_value=True) as registry:
            self.assertEqual(c.reconcile(REV, ['smoke']), 0)
            api.snapshots = [snapshot('supervisor'), snapshot('sandbox')]
            self.assertEqual(c.reconcile(REV, ['smoke']), 0)
            api.snapshots = [snapshot('e2e')]
            api.snapshots[0]['metadata']['creationTimestamp'] = '2026-11-22T10:00:00Z'
            self.assertEqual(c.reconcile(REV, ['smoke']), 1)
            for path in list(api.items):
                if api.items[path]['kind'] == 'PipelineRun':
                    del api.items[path]
            api.snapshots = []
            self.assertEqual(c.reconcile(REV, ['smoke']), 0)
            registry.assert_called_once()
        self.assertEqual(api.created_runs, 1)

    def test_dispatch_is_deduplicated_after_worker_run_is_pruned(self):
        api = FakeAPI()
        with patch.object(v, 'cluster_request', side_effect=api.request):
            self.assertTrue(c.dispatch(group(), REV))
            self.assertFalse(c.dispatch(group(), REV))
            name = c.worker(group(), REV, 1)['metadata']['name']
            del api.items[c.RUNS + '/' + name]
            self.assertFalse(c.dispatch(group(), REV))
        self.assertEqual(api.created_runs, 1)

    def test_lost_create_response_recovers_one_run_and_removes_finalizer(self):
        api = FakeAPI()
        api.lose_dispatch_response = True
        with patch.object(v, 'cluster_request', side_effect=api.request):
            with self.assertRaises(TimeoutError):
                c.dispatch(group(), REV)
            self.assertFalse(c.dispatch(group(), REV))
        self.assertEqual(api.created_runs, 1)
        run = next(item for item in api.items.values() if item['kind'] == 'PipelineRun')
        self.assertNotIn(c.FINALIZER, run['metadata']['finalizers'])

    def test_lost_group_annotation_response_recovers_dispatch(self):
        api = FakeAPI()
        api.lose_marker_response = True
        with patch.object(v, 'cluster_request', side_effect=api.request):
            with self.assertRaises(TimeoutError):
                c.dispatch(group(), REV)
            self.assertFalse(c.dispatch(group(), REV))
        self.assertEqual(api.created_runs, 1)
        run = next(item for item in api.items.values() if item['kind'] == 'PipelineRun')
        self.assertNotIn(c.FINALIZER, run['metadata']['finalizers'])

    def test_explicit_attempt_creates_a_new_run_but_never_claims_native_snapshot_validation(self):
        api = FakeAPI()
        with patch.object(v, 'cluster_request', side_effect=api.request):
            c.dispatch(group(), REV)
            c.dispatch(group(), REV, attempt=2)
        self.assertEqual(api.created_runs, 2)
        retained = [item for item in api.items.values() if item['kind'] == 'ConfigMap']
        self.assertEqual(len(retained), 1)
        self.assertEqual(len(retained[0]['metadata']['annotations']), 2)
        run = c.worker(group(), REV, 1)
        self.assertEqual({p['name'] for p in run['spec']['params']}, {'group-id', 'config-revision'})
        self.assertNotIn('test.appstudio.openshift.io/snapshot', run['metadata'].get('annotations', {}))
        self.assertEqual(run['spec']['pipelineRef']['params'][1]['value'], REV)

    def test_missing_or_superseded_registry_images_do_not_provision(self):
        def registry(url):
            if '/manifest/' in url:
                return {'manifest_data': json.dumps({'manifests': [{'platform': {'os': 'linux', 'architecture': 'amd64'}}]})}
            return {'tags': [{'name': TAG, 'manifest_digest': DIGEST}]}
        self.assertTrue(c.registry_ready(group(), registry))
        def missing(url):
            raise urllib.error.HTTPError(url, 404, 'missing', {}, None)
        self.assertFalse(c.registry_ready(group(), missing))
        def rebuilt(url):
            return registry(url) if '/manifest/' in url else {'tags': [{'name': TAG, 'manifest_digest': 'sha256:' + 'd' * 64}]}
        self.assertFalse(c.registry_ready(group(), rebuilt))


class WorkerContracts(unittest.TestCase):
    def test_prepare_requires_the_retained_canonical_group(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            (data / 'config-revision').write_text(REV)
            value = group()
            env = {'GROUP_ID': value['group_id'], 'RUN_ID': 'test-run', 'PIPELINE_UID': 'run-uid'}
            env.update({role.upper() + '_RESULT': str(data / role) for role in v.ROLES})
            env['TIERS_RESULT'] = str(data / 'tiers')
            cm = c.configmap('group', 'group', value)
            with patch.dict(v.os.environ, env, clear=True), patch.object(v, 'cluster_request', return_value=cm):
                v.prepare(SimpleNamespace(data=data))
                self.assertEqual((data / 'e2e').read_text(), value['images']['e2e']['image'])
                self.assertEqual((data / 'tiers').read_text(), 'smoke')
                cm['immutable'] = False
                with self.assertRaisesRegex(ValueError, 'retained immutable'):
                    v.prepare(SimpleNamespace(data=data))
                cm['immutable'] = True
                v.os.environ['GROUP_ID'] = 'c' * 64
                with self.assertRaisesRegex(ValueError, 'requested group ID'):
                    v.prepare(SimpleNamespace(data=data))

    def test_cleanup_task_is_independent_of_publisher_secret_and_workspaces(self):
        import yaml
        pipeline = yaml.safe_load((Path(__file__).parents[1] / 'version-tests.yaml').read_text())['spec']
        cleanup = next(task for task in pipeline['finally'] if task['name'] == 'cleanup-cluster')
        self.assertNotIn('workspaces', cleanup)
        self.assertNotIn('volumes', cleanup['taskSpec'])
        self.assertNotIn('runAfter', cleanup)
        compile(cleanup['taskSpec']['steps'][0]['script'], '<cleanup-task>', 'exec')
        self.assertEqual(pipeline['results'][0]['value'], '$(finally.report.results.TEST_OUTPUT)')

    def test_dependencies_are_prepared_before_cloud_provisioning_and_reporting_reuses_them(self):
        import yaml
        pipeline = yaml.safe_load((Path(__file__).parents[1] / 'version-tests.yaml').read_text())['spec']
        prepare, provision = pipeline['tasks'][:2]
        self.assertEqual(provision['runAfter'], ['prepare'])
        self.assertIn('requirements.txt', prepare['taskSpec']['steps'][0]['script'])
        self.assertIn("'pip', 'install'", prepare['taskSpec']['steps'][0]['script'])
        report = next(task for task in pipeline['finally'] if task['name'] == 'report')
        self.assertNotIn('bootstrap', [step['name'] for step in report['taskSpec']['steps']])
        self.assertNotIn('SNAPSHOT', [param['name'] for param in pipeline['params']])

    def test_onboarding_has_one_suspended_reconciler_without_recording_its(self):
        import yaml
        resources = list(yaml.safe_load_all((Path(__file__).parents[1] / 'onboarding.yaml').read_text()))
        self.assertNotIn('IntegrationTestScenario', [resource['kind'] for resource in resources])
        crons = [resource for resource in resources if resource['kind'] == 'CronJob']
        self.assertEqual(len(crons), 1)
        self.assertTrue(crons[0]['spec']['suspend'])
        self.assertEqual(sum(resource['kind'] == 'ServiceAccount' for resource in resources), 2)



if __name__ == '__main__':
    unittest.main()
