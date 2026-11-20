import base64
import copy
import unittest
import urllib.error

import yaml

from test_version_tests import images, manifest, m, TAG


def record():
    return dict(manifest(), images=images(), status='passed',
                counts=dict(passed=11, failed=0, errors=0, skipped=0, total=11),
                finished_at='2026-11-20T10:30:00Z')


class Git:
    def __init__(self, current=None):
        self.current = current
        self.head = 1
        self.trees = []
        self.writes = 0
        self.conflict_once = False
        self.concurrent_summary = None

    def api(self, url, **kwargs):
        if '/git/ref/heads/' in url:
            return {'object': {'sha': 'head-' + str(self.head)}}
        if '/git/commits/head-' in url:
            return {'tree': {'sha': 'tree-head-' + str(self.head)}}
        if '/contents/' in url:
            if self.current is None:
                raise urllib.error.HTTPError(url, 404, 'absent', {}, None)
            return {'content': base64.b64encode(yaml.safe_dump(self.current).encode()).decode()}
        if url.endswith('/git/trees'):
            self.trees.append(copy.deepcopy(kwargs['data']))
            return {'sha': 'new-tree'}
        if url.endswith('/git/commits'):
            return {'sha': 'new-commit'}
        if '/git/refs/heads/' in url:
            assert kwargs['method'] == 'PATCH' and kwargs['data']['force'] is False
            self.head += 1
            if self.conflict_once:
                self.conflict_once = False
                if self.concurrent_summary is not None:
                    self.current = self.concurrent_summary
                raise urllib.error.HTTPError(url, 422, 'not fast forward', {}, None)
            self.current = yaml.safe_load(self.trees[-1]['tree'][0]['content'])
            self.writes += 1
            return {}
        raise AssertionError(url)


class Publishing(unittest.TestCase):
    def test_one_yaml_file_uses_existing_early_gate_count_fields(self):
        git = Git()
        value = record()
        value['counts'] = dict(passed=8, failed=1, errors=1, skipped=1, total=11)
        value['status'] = 'failed'
        m.publish_record(value, 'unused', api=git.api)
        self.assertEqual(len(git.trees[0]['tree']), 1)
        self.assertEqual(git.trees[0]['tree'][0]['path'], f'openshell/{TAG}/early-gate-test-summary.yaml')
        self.assertEqual(git.current['test_summary'], dict(Failed=2, Passed=8, Skipped=1, Total=11))
        self.assertEqual(git.current['correlation_id'], value['run_id'])
        self.assertNotIn('counts', git.current)
        self.assertNotIn('snapshot', git.current)

    def test_same_attempt_is_idempotent_but_changed_record_is_rejected(self):
        git = Git()
        value = record()
        m.publish_record(value, 'unused', api=git.api)
        m.publish_record(value, 'unused', api=git.api)
        self.assertEqual(git.writes, 1)
        value['finished_at'] = '2026-11-20T11:30:00Z'
        with self.assertRaisesRegex(ValueError, 'existing attempt'):
            m.publish_record(value, 'unused', api=git.api)

    def test_newer_failed_or_infrastructure_rerun_replaces_previous_pass(self):
        for status in ('failed', 'error'):
            git = Git(m.public_summary(record()))
            newer = record()
            newer.update(run_id='rerun-2', started_at='2026-11-20T12:00:00Z', status=status)
            newer['counts'] = dict(passed=10, failed=1, errors=0, skipped=0, total=11)
            m.publish_record(newer, 'unused', api=git.api)
            self.assertEqual(git.current['status'], status)
            self.assertEqual(git.current['correlation_id'], 'rerun-2')

    def test_late_finishing_older_run_cannot_replace_newer_summary(self):
        newer = record()
        newer.update(run_id='newer', started_at='2026-11-20T12:00:00Z', status='error')
        git = Git(m.public_summary(newer))
        m.publish_record(record(), 'unused', api=git.api)
        self.assertEqual(git.writes, 0)
        self.assertEqual(git.current['status'], 'error')

    def test_timezone_offsets_are_compared_as_instants(self):
        newer = record()
        newer.update(run_id='newer', started_at='2026-11-19T14:30:00+02:00')
        git = Git(m.public_summary(newer))
        m.publish_record(record(), 'unused', api=git.api)
        self.assertEqual(git.writes, 0)

    def test_branch_conflict_rebases_one_file_on_latest_tree(self):
        git = Git()
        git.conflict_once = True
        m.publish_record(record(), 'unused', api=git.api)
        self.assertEqual(git.writes, 1)
        self.assertEqual(len(git.trees), 2)
        self.assertEqual(git.trees[-1]['base_tree'], 'tree-head-2')
        self.assertTrue(all(len(tree['tree']) == 1 for tree in git.trees))

    def test_newer_summary_winning_a_conflict_is_preserved(self):
        git = Git()
        git.conflict_once = True
        newer = record()
        newer.update(run_id='newer', started_at='2026-11-20T12:00:00Z')
        git.concurrent_summary = m.public_summary(newer)
        m.publish_record(record(), 'unused', api=git.api)
        self.assertEqual(git.writes, 0)
        self.assertEqual(git.current['correlation_id'], 'newer')

    def test_invalid_path_is_rejected_before_network(self):
        invalid = record()
        invalid['version'] = '../escape'
        with self.assertRaises(ValueError):
            m.publish_record(invalid, 'unused', api=lambda *a, **k: self.fail('network called'))
