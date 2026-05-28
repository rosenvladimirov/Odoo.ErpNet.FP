"""Driver subpackages for non-fiscal device bridges.

Co-tenant с `routes/`. Pattern: each driver subpackage exposes a registry
+ a per-device class with `async def call(...)` / `async def notifications()`
semantics. Routes in `routes/` are thin HTTP wrappers around drivers here.
"""
