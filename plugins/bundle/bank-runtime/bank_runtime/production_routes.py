"""Prune FastAPI route trees without dropping inclusion context or dependencies."""

from copy import copy

from fastapi.routing import iter_route_contexts


def route_paths(routes):
    return {context.path for context in iter_route_contexts(routes) if context.path}


def prune_routes(routes, *, allowed, explicit_ids, prefix=""):
    kept = []
    removed = 0
    for route in routes:
        if id(route) in explicit_ids:
            kept.append(route)
            continue
        # FastAPI 0.141 (locked in the production image) retains nested routers.
        # Copy only this inclusion so pruning an agent-scoped branch cannot
        # mutate the same router's unscoped inclusion or lose dependencies.
        original = getattr(route, "original_router", None)
        inclusion = getattr(route, "include_context", None)
        if original is not None and inclusion is not None:
            children, count = prune_routes(
                original.routes,
                allowed=allowed,
                explicit_ids=explicit_ids,
                prefix=prefix + inclusion.prefix,
            )
            removed += count
            if children:
                router = copy(original)
                router.routes = children
                kept.append(
                    type(route)(original_router=router, include_context=inclusion)
                )
            continue
        path = prefix + str(getattr(route, "path", ""))
        if path and allowed(path, set(getattr(route, "methods", ()) or ())):
            kept.append(route)
        else:
            removed += 1
    return kept, removed
