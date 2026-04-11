import sys
import types
import unittest

sys.modules.setdefault("uuid6", types.SimpleNamespace(uuid7=lambda: "00000000-0000-0000-0000-000000000000"))

from app.core.subscription_access import normalize_subscription_tier, user_can_access_tier


class SubscriptionAccessTests(unittest.TestCase):
    def test_normalize_subscription_tier_handles_adv_aliases(self) -> None:
        self.assertEqual(normalize_subscription_tier(" ADV-26 "), "adv26")
        self.assertEqual(normalize_subscription_tier("adv_26"), "adv26")
        self.assertEqual(normalize_subscription_tier("adv 26"), "adv26")

    def test_user_can_access_tier_supports_canonical_match(self) -> None:
        self.assertTrue(user_can_access_tier("adv-26", "adv26"))
        self.assertTrue(user_can_access_tier("ADV26", "adv-26"))

    def test_user_can_access_tier_respects_free_and_paid_rules(self) -> None:
        self.assertTrue(user_can_access_tier(None, "free"))
        self.assertTrue(user_can_access_tier("lite", None))
        self.assertFalse(user_can_access_tier(None, "adv26"))
        self.assertFalse(user_can_access_tier("lite", "adv26"))


if __name__ == "__main__":
    unittest.main()
