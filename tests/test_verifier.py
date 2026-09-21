import unittest

from store.verifier import verify_cancellation


class VerifyCancellationTests(unittest.TestCase):
    def test_final_status_examples(self) -> None:
        cases = (
            ("processing", "canceled", "canceled", True),
            ("processing", "processing", "canceled", False),
            ("shipped", "shipped", "shipped", True),
            ("shipped", "canceled", "shipped", False),
        )

        for before, after, expected, result in cases:
            with self.subTest(before=before, after=after, expected=expected):
                self.assertEqual(
                    verify_cancellation(before, after, expected),
                    result,
                )


if __name__ == "__main__":
    unittest.main()
