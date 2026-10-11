"""Executado diariamente pelo GitHub Actions (também pode rodar manualmente)."""
import core

core.init_db()
print(core.run_ingest())
