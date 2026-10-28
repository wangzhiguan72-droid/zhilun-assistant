"""Independent t report formatting must preserve computed values."""
from pathlib import Path
import unittest

import numpy as np
import pandas as pd
from scipy import stats

from app import run_independent_t


class IndependentTDisplayTest(unittest.TestCase):
    def assert_numeric_result(self, data, result):
        groups = [g['score'].to_numpy() for _, g in data.groupby('gender', sort=False)]
        equal = stats.levene(*groups).pvalue > .05
        expected = stats.ttest_ind(*groups, equal_var=equal)
        self.assertEqual(result['summary']['p'], float(expected.pvalue))
        self.assertEqual(result['summary']['t'], float(expected.statistic))
        return float(expected.pvalue)

    def test_real_demo_uses_threshold_and_no_duplicate_difference(self):
        data = pd.read_csv(Path(__file__).parent / 'examples/student_scores.csv')
        result = run_independent_t(data, 'gender', 'score')
        p = self.assert_numeric_result(data, result)
        self.assertLess(p, .001)
        self.assertIn('| p 值 | <0.001 |', result['markdown'])
        self.assertIn('p < 0.001，Cohen', result['markdown'])
        self.assertNotIn('p = 0.000', result['markdown'])
        self.assertNotIn('差异差异', result['markdown'])

    def test_non_significant_report_keeps_actual_rounded_p(self):
        # Deterministic finite samples with a modest mean shift, no transport mock.
        base = np.array([-2., -1., 0., 1., 2.])
        data = pd.DataFrame({'gender': ['A']*5 + ['B']*5,
                             'score': np.r_[base, base + .8]})
        result = run_independent_t(data, 'gender', 'score')
        p = self.assert_numeric_result(data, result)
        self.assertGreater(p, .05)
        rounded = f'{p:.3f}'
        self.assertIn(f'| p 值 | {rounded} |', result['markdown'])
        self.assertIn(f'p = {rounded}，Cohen', result['markdown'])
        self.assertNotIn('差异差异', result['markdown'])

    def test_very_small_levene_p_uses_threshold_and_preserves_float(self):
        data = pd.DataFrame({'gender': ['A']*20 + ['B']*20,
                             'score': np.r_[np.linspace(-.01, .01, 20),
                                            np.linspace(-10., 10., 20)]})
        result = run_independent_t(data, 'gender', 'score')
        expected = float(stats.levene(data['score'][:20], data['score'][20:]).pvalue)
        self.assertLess(expected, .001)
        self.assertEqual(result['summary']['levene_p'], expected)
        self.assertIn('p < 0.001\n- 结论：方差不齐', result['markdown'])
        self.assertNotIn('p = 0.000', result['markdown'])


if __name__ == '__main__':
    unittest.main()
