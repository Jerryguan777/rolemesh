"""``webui.v1`` — handlers backing the ``/api/v1/*`` REST surface.

Per webui-backend-v1.1 design §3 the v1 surface is independent from
the legacy ``/api/admin/*`` router; helpers/handlers live in this
package rather than re-exporting from :mod:`webui.admin`. Cross-cutting
concerns (error envelope, dependencies) sit under ``webui.v1``
submodules so each endpoint module can import only what it needs.
"""
