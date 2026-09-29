"""GUI web package: FastAPI surface over the smc_engine hub.

All safety (account gate, order-type allow-list, risk validation, portfolio
budget, ticket ownership) is enforced in the backend. The browser only sends
intent through these endpoints and renders whatever comes back.
"""
