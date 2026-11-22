import unittest

from gpu_gate.window import Window


class WindowTest(unittest.TestCase):
    def test_empty_mean_is_none(self):
        self.assertIsNone(Window(1800).mean())
        self.assertEqual(len(Window(1800)), 0)

    def test_mean_over_samples_present_while_filling(self):
        w = Window(1800)
        w.add(0, 100)
        self.assertEqual(w.mean(), 100)
        w.add(5, 0)
        self.assertEqual(w.mean(), 50)
        w.add(10, 20)
        self.assertAlmostEqual(w.mean(), 40)
        self.assertEqual(len(w), 3)

    def test_expiry_boundary_sample_exactly_window_old_is_dropped(self):
        w = Window(1800)
        w.add(0, 100)
        w.add(1799, 0)
        self.assertEqual(len(w), 2)
        w.add(1800, 0)  # the t=0 sample is now exactly 1800 s old -> out
        self.assertEqual(len(w), 2)
        self.assertEqual(w.mean(), 0)

    def test_steady_state_holds_360_samples_at_5s(self):
        w = Window(1800)
        for i in range(1000):
            w.add(i * 5, i)
        self.assertEqual(len(w), 360)
        last = list(range(1000 - 360, 1000))
        self.assertAlmostEqual(w.mean(), sum(last) / 360)

    def test_expire_without_add_drains(self):
        w = Window(30)
        w.add(0, 80)
        w.add(10, 80)
        w.expire(35)
        self.assertEqual(len(w), 1)
        w.expire(100)
        self.assertIsNone(w.mean())


if __name__ == "__main__":
    unittest.main()
