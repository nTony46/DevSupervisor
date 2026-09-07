"""Nothing in the default path or the test suite can spend money."""

from types import SimpleNamespace

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

    def test_prior_campaign_spend_carries_into_a_new_process(self):
        """A cap spans supervisor processes; a provider starting at zero enforces nothing."""
        provider = providers.resolve("claude-cli", allow_paid=True, budget_usd=35.0,
                                     spent_usd=34.0)
        self.assertEqual(provider.spent_usd, 34.0)

    def test_a_run_whose_own_ceiling_overruns_the_cap_is_refused_before_dispatch(self):
        # Arrange: $3 left, and a request authorised to spend up to $12.
        provider = providers.resolve("claude-cli", allow_paid=True, budget_usd=35.0,
                                     spent_usd=32.0)
        request = SimpleNamespace(max_budget_usd=12.0)

        # Act / Assert
        with self.assertRaises(BudgetExceeded) as caught:
            provider.preflight(request)
        self.assertIn("does not fit the remaining $3.00", str(caught.exception))

    def test_a_run_that_fits_the_remaining_budget_is_not_refused_for_cost(self):
        provider = providers.resolve("claude-cli", allow_paid=True, budget_usd=35.0,
                                     spent_usd=32.0)
        try:
            provider.preflight(SimpleNamespace(max_budget_usd=3.0))
        except BudgetExceeded:                       # pragma: no cover - the failure
            self.fail("a request inside the remaining budget must not be refused")
        except Exception:                            # the CLI may be absent; not our subject
            pass
