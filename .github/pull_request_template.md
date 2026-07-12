## What & why

## Checklist
- [ ] `make lint && make test` pass (tests must not need cloud credentials)
- [ ] Adapters: lifecycle states are honest; cancels poll to terminal or set `leaked`
- [ ] New user-facing API: short design note below on why Router/policies/validators can't already express it
- [ ] Numbers/claims name their cost model (docs/cost-accounting.md)
