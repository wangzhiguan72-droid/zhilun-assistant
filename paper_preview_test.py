"""论文准备阶段不运行核查，不借损坏文件制造已准备状态。"""
import io
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('RATE_LIMIT_DISABLE', '1')
from app import app


class PaperPreviewTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_preview_does_not_lint(self):
        with patch('paper_lint.lint_paper', side_effect=AssertionError('preview must not check')):
            r = self.client.post('/api/paper_preview', data={'paper': (io.BytesIO('自造论文正文。'.encode()), 'sample.txt')})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json['ok'])
        self.assertEqual(len(r.json['sha256']), 64)

    def test_invalid_empty_encrypted_input_not_ready(self):
        for name, raw in [('bad.docx', b'not zip'), ('empty.txt', b''), ('old.doc', b'text')]:
            r = self.client.post('/api/paper_preview', data={'paper': (io.BytesIO(raw), name)})
            self.assertEqual(r.status_code, 400)
            self.assertFalse(r.json['ok'])

    def test_malformed_explicit_model_policy_stops_before_compute(self):
        with patch('app.call_method', side_effect=AssertionError('must not compute')):
            r = self.client.post('/api/analyze', json={'ai_policy': {'schema_version': 99}, 'file_id': 'missing', 'use_llm':1})
        self.assertEqual(r.status_code, 400)

    def test_invalid_ai_settings_do_not_block_local_analysis(self):
        r = self.client.post('/api/upload', data={'file': (io.BytesIO(b'group,score\na,1\na,2\na,3\nb,4\nb,5\nb,6\n'), 'data.csv')})
        self.assertTrue(r.json['ok'])
        r = self.client.post('/api/analyze', json={'file_id': r.json['file_id'], 'method':'independent_t', 'group_col':'group', 'value_col':'score', 'ai_policy':{'schema_version':99}, 'use_llm':0})
        self.assertEqual(r.status_code, 200, r.json)
        self.assertTrue(r.json['ok'])

if __name__ == '__main__':
    unittest.main()
