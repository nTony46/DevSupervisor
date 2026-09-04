"""Nothing in the default path or the test suite can spend money."""

from devsupervisor import providers
from devsupervisor.providers import BudgetExceeded
from devsupervisor.supervisor import Supervisor
from tests.support import HarnessTestCase


class NoPaidCallsTests(HarnessTestCase):
    def test_default_provider_is_free(self):
        provider = providers.resolve()
        self.assertFalse(provider.is_paid)
        self.assertEqual(provider.name, "mock")

    def test_supervisor_defaults_to_the_free_provider(self):
        supervisor = Supervisor(self.store)
        self.assertFalse(supervisor.provider.is_paid)

    def test_a_paid_provider_needs_an_explicit_opt_in(self):
        with self.assertRaises(BudgetExceeded):
            providers.resolve("claude-cli")

    def test_a_paid_provider_needs_a_budget_before_it_runs(self):
        provider = providers.resolve("claude-cli", allow_paid=True, budget_usd=0)
        with self.assertRaises(BudgetExceeded):
            provider.preflight()

    def test_an_exhausted_budget_stops_further_runs(self):
        provider = providers.resolve("claude-cli", allow_paid=True, budget_usd=1.0)
        provider.spent_usd = 1.5
        with self.assertRaises(BudgetExceeded):
            provider.preflight()
