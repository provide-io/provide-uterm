//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Http.Json;
using System.Net.Sockets;
using System.Text.Json;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Def = Provide.Uterm.Server.GraphicalTargetDefinition;

namespace Provide.Uterm.Tests.Gui;

/// <summary>
/// Coverage for the tenant-scoped graphical-target registry + REST surface
/// (canonical model, no min_role). Runs in the ~Gui gate batch.
/// </summary>
public class GraphicalTargetsRestTests
{
    private const string Tenant = "acme";

    // ---- Registry / model unit tests -------------------------------------

    [Fact]
    public async Task Scope_TryForTenant_And_System()
    {
        Assert.False(GraphicalTargetScope.TryForTenant("", out _));
        Assert.False(GraphicalTargetScope.TryForTenant("   ", out _));
        Assert.True(GraphicalTargetScope.TryForTenant(Tenant, out var s));
        Assert.Equal(Tenant, s.TenantId);
        Assert.False(s.IsSystem);
        Assert.True(s.IsValid);
        Assert.True(s.Permits(Tenant));
        Assert.False(s.Permits("other"));
        Assert.False(s.Permits(null));

        var sys = GraphicalTargetScope.System();
        Assert.True(sys.IsSystem);
        Assert.True(sys.IsValid);
        Assert.True(sys.Permits("anything"));
        Assert.True(sys.Permits(null));

        var invalid = default(GraphicalTargetScope);
        Assert.False(invalid.IsValid);
        Assert.False(invalid.Permits(Tenant));
    }

    [Fact]
    public async Task Definition_Validate_Rejects_BadInputs()
    {
        Assert.Throws<ArgumentException>(() => new Def { TargetId = "bad id!", Protocol = "memory" }.Validate());
        Assert.Throws<ArgumentException>(() => new Def { TargetId = "ok", Protocol = "ftp" }.Validate());
        Assert.Throws<ArgumentException>(() => new Def { TargetId = "ok", Protocol = "memory", Width = 0 }.Validate());
        Assert.Throws<ArgumentException>(() => new Def { TargetId = "ok", Protocol = "memory", Width = 100_000 }.Validate());
        Assert.Throws<ArgumentException>(() => new Def { TargetId = "ok", Protocol = "memory", Height = 0 }.Validate());
        Assert.Throws<ArgumentException>(() => new Def { TargetId = "ok", Protocol = "memory", Height = 100_000 }.Validate());
        Assert.Throws<ArgumentException>(() =>
            new Def { TargetId = "ok", Protocol = "memory", TenantId = "bad tenant!" }.Validate());
        Assert.Throws<ArgumentException>(() =>
            new Def { TargetId = "ok", Protocol = "memory", CaSecretRef = "not-a-ref" }.Validate()); // pragma: allowlist secret
    }

    [Fact]
    public async Task Definition_Validate_Normalizes_RfbEndpoint()
    {
        var d = new Def { TargetId = "ok", Protocol = "RFB", Endpoint = "rfb://host.example:5901", Width = 10, Height = 10 };
        d.Validate();
        Assert.Equal("rfb", d.Protocol);
        Assert.Equal("host.example:5901", d.Endpoint);
    }

    [Fact]
    public async Task Definition_Clone_And_PublicCopy_StripSecrets()
    {
        var d = new Def
        {
            TargetId = "ok",
            Protocol = "memory",
            Secret = "s3cret", // pragma: allowlist secret
            CaSecretRef = "env:CA", // pragma: allowlist secret
            ClientCertSecretRef = "env:CERT", // pragma: allowlist secret
            ClientKeySecretRef = "env:KEY", // pragma: allowlist secret
            CreatedBy = "me",
        };
        var clone = d.Clone();
        Assert.Equal("s3cret", clone.Secret);
        Assert.Equal("me", clone.CreatedBy);

        var pub = d.PublicCopy();
        Assert.Null(pub.Secret);
        Assert.Null(pub.CaSecretRef);
        Assert.Null(pub.ClientCertSecretRef);
        Assert.Null(pub.ClientKeySecretRef);
    }

    [Fact]
    public async Task ParseRfbEndpoint_Branches()
    {
        Assert.Equal(("h", 5900), GraphicalTargetParsing.ParseRfbEndpoint("h:5900"));
        Assert.Equal(("h", 5900), GraphicalTargetParsing.ParseRfbEndpoint("rfb://h:5900"));
        Assert.Equal(("h", 5900), GraphicalTargetParsing.ParseRfbEndpoint("dns:///h:5900"));

        foreach (var bad in new[] { "", "   ", "hostonly", "rfb://", "rfb://h:0", "rfb://h:99999" })
        {
            var ex = Assert.Throws<GraphicalTargetException>(() => GraphicalTargetParsing.ParseRfbEndpoint(bad));
            Assert.Equal(GraphicalTargetErrorCode.Invalid, ex.Code);
        }
    }

    [Fact]
    public async Task Registry_StaticCtor_And_Duplicates()
    {
        var reg = new InMemoryGraphicalTargetRegistry(new[]
        {
            new Def { TargetId = "s1", Protocol = "memory", Width = 10, Height = 10 },
        });
        var sys = GraphicalTargetScope.System();
        Assert.NotNull(await reg.GetAsync(sys, "s1"));
        Assert.Single(await reg.ListAsync(sys));

        Assert.Throws<InvalidOperationException>(() => new InMemoryGraphicalTargetRegistry(new[]
        {
            new Def { TargetId = "dup", Protocol = "memory", Width = 1, Height = 1 },
            new Def { TargetId = "dup", Protocol = "memory", Width = 1, Height = 1 },
        }));

        await reg.AddStaticAsync(new Def { TargetId = "s2", Protocol = "memory", Width = 1, Height = 1 });
        await Assert.ThrowsAsync<InvalidOperationException>(async () => await reg.AddStaticAsync(new Def { TargetId = "s2", Protocol = "memory", Width = 1, Height = 1 }));
    }

    [Fact]
    public async Task Registry_Crud_TenantScoped()
    {
        var reg = new InMemoryGraphicalTargetRegistry();
        Assert.True(GraphicalTargetScope.TryForTenant(Tenant, out var scope));
        Assert.True(GraphicalTargetScope.TryForTenant("other", out var otherScope));

        var created = await reg.CreateAsync(scope, new Def
        {
            TargetId = "gt-1",
            TenantId = Tenant,
            Protocol = "memory",
            Width = 20,
            Height = 20,
            CreatedBy = "creator",
        });
        Assert.Equal(Tenant, created.TenantId);

        // Get / List honor scope.
        Assert.NotNull(await reg.GetAsync(scope, "gt-1"));
        Assert.Null(await reg.GetAsync(scope, "missing"));
        Assert.Null(await reg.GetAsync(otherScope, "gt-1"));
        Assert.Single(await reg.ListAsync(scope));
        Assert.Empty(await reg.ListAsync(otherScope));

        // Duplicate create.
        var dup = await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.CreateAsync(scope, new Def { TargetId = "gt-1", TenantId = Tenant, Protocol = "memory", Width = 1, Height = 1 }));
        Assert.Equal(GraphicalTargetErrorCode.AlreadyExists, dup.Code);

        // Create with tenant that the scope does not permit.
        var forbid = await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.CreateAsync(scope, new Def { TargetId = "gt-x", TenantId = "other", Protocol = "memory", Width = 1, Height = 1 }));
        Assert.Equal(GraphicalTargetErrorCode.Forbidden, forbid.Code);

        // Invalid definition surfaces as Invalid.
        var invalid = await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.CreateAsync(scope, new Def { TargetId = "gt-y", TenantId = Tenant, Protocol = "memory", Width = 0, Height = 1 }));
        Assert.Equal(GraphicalTargetErrorCode.Invalid, invalid.Code);

        // Update preserves CreatedBy even when the payload omits it (PUT semantics).
        var updated = await reg.UpdateAsync(scope, new Def
        {
            TargetId = "gt-1",
            TenantId = Tenant,
            Protocol = "memory",
            Width = 30,
            Height = 30,
            DisplayName = "renamed",
            CreatedBy = null,
        });
        Assert.Equal("renamed", updated.DisplayName);
        Assert.Equal("creator", updated.CreatedBy);
        Assert.NotNull(updated.UpdatedAt);

        // Update missing / other-tenant.
        var notFound = await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.UpdateAsync(scope, new Def { TargetId = "nope", TenantId = Tenant, Protocol = "memory", Width = 1, Height = 1 }));
        Assert.Equal(GraphicalTargetErrorCode.NotFound, notFound.Code);

        var updForbid = await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.UpdateAsync(otherScope, new Def { TargetId = "gt-1", TenantId = "other", Protocol = "memory", Width = 1, Height = 1 }));
        Assert.Equal(GraphicalTargetErrorCode.Forbidden, updForbid.Code);

        // Delete missing / other-tenant / success.
        var delMissing = await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.DeleteAsync(scope, "nope"));
        Assert.Equal(GraphicalTargetErrorCode.NotFound, delMissing.Code);
        var delForbid = await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.DeleteAsync(otherScope, "gt-1"));
        Assert.Equal(GraphicalTargetErrorCode.Forbidden, delForbid.Code);
        await reg.DeleteAsync(scope, "gt-1");
        Assert.Null(await reg.GetAsync(scope, "gt-1"));
    }

    [Fact]
    public async Task Registry_Static_Is_Immutable()
    {
        var reg = new InMemoryGraphicalTargetRegistry();
        await reg.AddStaticAsync(new Def { TargetId = "sys-1", TenantId = Tenant, Protocol = "memory", Width = 5, Height = 5 });
        Assert.True(GraphicalTargetScope.TryForTenant(Tenant, out var scope));

        var upd = await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.UpdateAsync(scope, new Def { TargetId = "sys-1", TenantId = Tenant, Protocol = "memory", Width = 6, Height = 6 }));
        Assert.Equal(GraphicalTargetErrorCode.Immutable, upd.Code);

        var del = await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.DeleteAsync(scope, "sys-1"));
        Assert.Equal(GraphicalTargetErrorCode.Immutable, del.Code);
    }

    [Fact]
    public async Task Registry_Forbidden_Tenant_Branches()
    {
        var reg = new InMemoryGraphicalTargetRegistry();
        await reg.AddStaticAsync(new Def { TargetId = "sys-t", TenantId = Tenant, Protocol = "memory", Width = 5, Height = 5 });
        Assert.True(GraphicalTargetScope.TryForTenant(Tenant, out var scope));
        Assert.True(GraphicalTargetScope.TryForTenant("other", out var otherScope));

        // Create with a tenant the scope does not permit → Forbidden.
        Assert.Equal(GraphicalTargetErrorCode.Forbidden, (await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.CreateAsync(scope, new Def { TargetId = "gt-z", TenantId = "other", Protocol = "memory", Width = 1, Height = 1 }))).Code);

        // Update with a payload tenant the scope does not permit → Forbidden (before lookup).
        await reg.CreateAsync(scope, new Def { TargetId = "gt-w", TenantId = Tenant, Protocol = "memory", Width = 1, Height = 1 });
        Assert.Equal(GraphicalTargetErrorCode.Forbidden, (await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.UpdateAsync(scope, new Def { TargetId = "gt-w", TenantId = "other", Protocol = "memory", Width = 1, Height = 1 }))).Code);

        // Delete a static target the scope does not permit → Forbidden.
        Assert.Equal(GraphicalTargetErrorCode.Forbidden,
            (await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.DeleteAsync(otherScope, "sys-t"))).Code);
    }

    [Fact]
    public async Task Registry_InvalidScope_Throws_Forbidden()
    {
        var reg = new InMemoryGraphicalTargetRegistry();
        var bad = default(GraphicalTargetScope);
        Assert.Equal(GraphicalTargetErrorCode.Forbidden,
            (await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.GetAsync(bad, "x"))).Code);
        Assert.Equal(GraphicalTargetErrorCode.Forbidden,
            (await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.ListAsync(bad))).Code);
        Assert.Equal(GraphicalTargetErrorCode.Forbidden,
            (await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.DeleteAsync(bad, "x"))).Code);
    }

    // ---- REST surface tests ---------------------------------------------

    [Fact]
    public async Task Rest_Crud_RoundTrip()
    {
        await using var h = await Harness.StartAsync();
        using var client = h.Client();

        // Create.
        var create = await client.PostAsync("/api/graphical-targets", Json(new
        {
            protocol = "memory",
            display_name = "My Screen",
            width = 64,
            height = 48,
        }));
        Assert.Equal(HttpStatusCode.Created, create.StatusCode);
        var created = await ReadJson(create);
        var id = created.GetProperty("target_id").GetString()!;
        Assert.Equal(Tenant, created.GetProperty("tenant_id").GetString());
        Assert.False(created.TryGetProperty("secret", out _)); // secret stripped/omitted

        // List includes it.
        var list = await client.GetAsync("/api/graphical-targets?limit=50&offset=0");
        Assert.Equal(HttpStatusCode.OK, list.StatusCode);
        var listBody = await ReadJson(list);
        Assert.Equal(1, listBody.GetProperty("total").GetInt32());

        // Get by id.
        var get = await client.GetAsync($"/api/graphical-targets/{id}");
        Assert.Equal(HttpStatusCode.OK, get.StatusCode);

        // Update.
        var put = await client.PutAsync($"/api/graphical-targets/{id}",
            Json(new { protocol = "memory", display_name = "Renamed", width = 80, height = 60 }));
        Assert.Equal(HttpStatusCode.OK, put.StatusCode);
        var putBody = await ReadJson(put);
        Assert.Equal("Renamed", putBody.GetProperty("display_name").GetString());

        // Update with empty display_name keeps prior name.
        var put2 = await client.PutAsync($"/api/graphical-targets/{id}",
            Json(new { protocol = "memory", width = 80, height = 60 }));
        Assert.Equal(HttpStatusCode.OK, put2.StatusCode);
        Assert.Equal("Renamed", (await ReadJson(put2)).GetProperty("display_name").GetString());

        // Delete.
        var del = await client.DeleteAsync($"/api/graphical-targets/{id}");
        Assert.Equal(HttpStatusCode.NoContent, del.StatusCode);

        // Gone.
        Assert.Equal(HttpStatusCode.NotFound, (await client.GetAsync($"/api/graphical-targets/{id}")).StatusCode);
    }

    [Fact]
    public async Task Rest_Create_Validation_Errors()
    {
        await using var h = await Harness.StartAsync();
        using var client = h.Client();

        // Unknown payload key.
        Assert.Equal(422, (int)(await client.PostAsync("/api/graphical-targets",
            Json(new { bogus = "x" }))).StatusCode);

        // tenant_id supplied → 422.
        Assert.Equal(422, (int)(await client.PostAsync("/api/graphical-targets",
            Json(new { protocol = "memory", tenant_id = "x", width = 4, height = 4 }))).StatusCode);

        // target_id supplied → 422.
        Assert.Equal(422, (int)(await client.PostAsync("/api/graphical-targets",
            Json(new { protocol = "memory", target_id = "abc", width = 4, height = 4 }))).StatusCode);

        // width out of range → 422.
        Assert.Equal(422, (int)(await client.PostAsync("/api/graphical-targets",
            Json(new { protocol = "memory", width = 100000, height = 1 }))).StatusCode);

        // width as fractional number → 422 (GetInt rejects non-int32).
        Assert.Equal(422, (int)(await client.PostRawAsync("/api/graphical-targets",
            "{\"protocol\":\"memory\",\"width\":1.5,\"height\":4}")).StatusCode);

        // width beyond int32 → 422.
        Assert.Equal(422, (int)(await client.PostRawAsync("/api/graphical-targets",
            "{\"protocol\":\"memory\",\"width\":99999999999,\"height\":4}")).StatusCode);

        // protocol as non-string → 422 (GetString rejects).
        Assert.Equal(422, (int)(await client.PostRawAsync("/api/graphical-targets",
            "{\"protocol\":123,\"width\":4,\"height\":4}")).StatusCode);

        // rfb without endpoint → 422.
        Assert.Equal(422, (int)(await client.PostAsync("/api/graphical-targets",
            Json(new { protocol = "rfb", width = 4, height = 4 }))).StatusCode);

        // width as numeric string parses fine → 201.
        Assert.Equal(HttpStatusCode.Created, (await client.PostRawAsync("/api/graphical-targets",
            "{\"protocol\":\"memory\",\"width\":\"50\",\"height\":\"40\"}")).StatusCode);
    }

    [Fact]
    public async Task Rest_Create_Field_Coercions()
    {
        await using var h = await Harness.StartAsync();
        using var client = h.Client();

        // Explicit JSON nulls fall back (display_name→"" then default, width→default).
        var withNulls = await client.PostRawAsync("/api/graphical-targets",
            "{\"protocol\":\"memory\",\"display_name\":null,\"secret\":null,\"width\":null,\"height\":48}");
        Assert.Equal(HttpStatusCode.Created, withNulls.StatusCode);
        var body = await ReadJson(withNulls);
        Assert.Equal("graphical-target", body.GetProperty("display_name").GetString());
        Assert.Equal(640, body.GetProperty("width").GetInt32());

        // Non-numeric width string → GetInt rejects → 422.
        Assert.Equal(422, (int)(await client.PostRawAsync("/api/graphical-targets",
            "{\"protocol\":\"memory\",\"width\":\"abc\",\"height\":4}")).StatusCode);
    }

    [Fact]
    public async Task Rest_List_Pagination_Validation()
    {
        await using var h = await Harness.StartAsync();
        using var client = h.Client();

        Assert.Equal(422, (int)(await client.GetAsync("/api/graphical-targets?limit=0")).StatusCode);
        Assert.Equal(422, (int)(await client.GetAsync("/api/graphical-targets?limit=9999")).StatusCode);
        Assert.Equal(422, (int)(await client.GetAsync("/api/graphical-targets?limit=abc")).StatusCode);
        Assert.Equal(422, (int)(await client.GetAsync("/api/graphical-targets?offset=-1")).StatusCode);

        // Offset beyond total clamps to empty page (still 200).
        var big = await client.GetAsync("/api/graphical-targets?offset=999");
        Assert.Equal(HttpStatusCode.OK, big.StatusCode);
    }

    [Fact]
    public async Task Rest_Update_MismatchAndMissing()
    {
        await using var h = await Harness.StartAsync();
        using var client = h.Client();

        // target_id in body != path → 409.
        var mismatch = await client.PutAsync("/api/graphical-targets/gt-abc",
            Json(new { protocol = "memory", target_id = "gt-different", width = 4, height = 4 }));
        Assert.Equal(HttpStatusCode.Conflict, mismatch.StatusCode);

        // Not found → 404.
        var missing = await client.PutAsync("/api/graphical-targets/gt-missing",
            Json(new { protocol = "memory", width = 4, height = 4 }));
        Assert.Equal(HttpStatusCode.NotFound, missing.StatusCode);

        // Delete missing → 404.
        Assert.Equal(HttpStatusCode.NotFound, (await client.DeleteAsync("/api/graphical-targets/gt-missing")).StatusCode);
    }

    [Fact]
    public async Task Rest_Access_Denied_Without_Tenant()
    {
        // Admin token with NO tenant claim → scope cannot be resolved → 403 on every verb.
        await using var h = await Harness.StartAsync(tenant: null);
        using var client = h.Client();
        Assert.Equal(HttpStatusCode.Forbidden, (await client.GetAsync("/api/graphical-targets")).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, (await client.GetAsync("/api/graphical-targets/gt-x")).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, (await client.PostAsync("/api/graphical-targets",
            Json(new { protocol = "memory", width = 4, height = 4 }))).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, (await client.PutAsync("/api/graphical-targets/gt-x",
            Json(new { protocol = "memory", width = 4, height = 4 }))).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, (await client.DeleteAsync("/api/graphical-targets/gt-x")).StatusCode);
    }

    [Fact]
    public async Task Rest_Manage_Requires_Capability()
    {
        // Viewer has graphical.target.read but NOT graphical.target.manage.
        await using var h = await Harness.StartAsync(roles: new[] { "viewer" });
        using var client = h.Client();

        // Read is allowed.
        Assert.Equal(HttpStatusCode.OK, (await client.GetAsync("/api/graphical-targets")).StatusCode);

        // Mutations are denied at the capability gate → 403.
        Assert.Equal(HttpStatusCode.Forbidden, (await client.PostAsync("/api/graphical-targets",
            Json(new { protocol = "memory", width = 4, height = 4 }))).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, (await client.PutAsync("/api/graphical-targets/gt-x",
            Json(new { protocol = "memory", width = 4, height = 4 }))).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, (await client.DeleteAsync("/api/graphical-targets/gt-x")).StatusCode);
    }

    [Fact]
    public async Task Rest_Update_Error_Branches()
    {
        await using var h = await Harness.StartAsync();
        using var client = h.Client();

        // Seed a target to update.
        var create = await client.PostAsync("/api/graphical-targets",
            Json(new { protocol = "memory", display_name = "seed", width = 32, height = 32 }));
        var id = (await ReadJson(create)).GetProperty("target_id").GetString()!;

        // Unknown key → 422.
        Assert.Equal(422, (int)(await client.PutAsync($"/api/graphical-targets/{id}",
            Json(new { bogus = 1 }))).StatusCode);

        // tenant_id in body → 422.
        Assert.Equal(422, (int)(await client.PutAsync($"/api/graphical-targets/{id}",
            Json(new { protocol = "memory", tenant_id = "x", width = 4, height = 4 }))).StatusCode);

        // Non-string protocol → parse error → 422.
        Assert.Equal(422, (int)(await client.PostRawAsync($"/api/graphical-targets/{id}",
            "{\"protocol\":5}", HttpMethod.Put)).StatusCode);

        // Invalid width on an existing target → registry validation → 422.
        Assert.Equal(422, (int)(await client.PutAsync($"/api/graphical-targets/{id}",
            Json(new { protocol = "memory", width = 100000, height = 4 }))).StatusCode);
    }

    [Fact]
    public async Task Rest_Static_Target_Is_Immutable()
    {
        var reg = new InMemoryGraphicalTargetRegistry();
        await reg.AddStaticAsync(new Def { TargetId = "sys-a", TenantId = Tenant, Protocol = "memory", Width = 10, Height = 10 });

        await using var h = await Harness.StartAsync(graphicalTargets: reg);
        using var client = h.Client();

        // Visible via list + get.
        Assert.Equal(1, (await ReadJson(await client.GetAsync("/api/graphical-targets"))).GetProperty("total").GetInt32());
        Assert.Equal(HttpStatusCode.OK, (await client.GetAsync("/api/graphical-targets/sys-a")).StatusCode);

        // Immutable → 409 on update + delete.
        Assert.Equal(HttpStatusCode.Conflict, (await client.PutAsync("/api/graphical-targets/sys-a",
            Json(new { protocol = "memory", display_name = "no", width = 12, height = 12 }))).StatusCode);
        Assert.Equal(HttpStatusCode.Conflict, (await client.DeleteAsync("/api/graphical-targets/sys-a")).StatusCode);
    }

    [Fact]
    public async Task Definition_Litevirt_Validate_RequiresEndpoint()
    {
        // litevirt is a supported canonical protocol; endpoint is required + normalized.
        var ok = new Def { TargetId = "vm", Protocol = "LITEVIRT", Endpoint = "dns:///10.0.0.5:7443", Width = 8, Height = 8 };
        ok.Validate();
        Assert.Equal("litevirt", ok.Protocol);
        Assert.Equal("10.0.0.5:7443", ok.Endpoint);

        // Missing endpoint → Invalid.
        var missing = Assert.Throws<GraphicalTargetException>(() =>
            new Def { TargetId = "vm", Protocol = "litevirt", Endpoint = null, Width = 8, Height = 8 }.Validate());
        Assert.Equal(GraphicalTargetErrorCode.Invalid, missing.Code);

        // Endpoint without a port → Invalid.
        var noPort = Assert.Throws<GraphicalTargetException>(() =>
            new Def { TargetId = "vm", Protocol = "litevirt", Endpoint = "hostonly", Width = 8, Height = 8 }.Validate());
        Assert.Equal(GraphicalTargetErrorCode.Invalid, noPort.Code);
    }

    [Fact]
    public async Task ParseLitevirtEndpoint_Branches()
    {
        Assert.Equal(("h", 7443), GraphicalTargetParsing.ParseLitevirtEndpoint("h:7443"));
        Assert.Equal(("h", 7443), GraphicalTargetParsing.ParseLitevirtEndpoint("dns:///h:7443"));
        foreach (var bad in new[] { "", "   ", "hostonly", "h:0", "h:99999" })
        {
            var ex = Assert.Throws<GraphicalTargetException>(() => GraphicalTargetParsing.ParseLitevirtEndpoint(bad));
            Assert.Equal(GraphicalTargetErrorCode.Invalid, ex.Code);
        }
    }

    [Fact]
    public async Task Definition_Config_Survives_Clone_And_PublicCopy()
    {
        var d = new Def { TargetId = "vm", Protocol = "memory" };
        d.Config["vm_name"] = "web-1";

        var clone = d.Clone();
        Assert.Equal("web-1", clone.Config["vm_name"]);
        // Deep copy: mutating the clone's map does not touch the original.
        clone.Config["vm_name"] = "changed";
        Assert.Equal("web-1", d.Config["vm_name"]);

        // Config is NOT a secret — PublicCopy keeps it.
        var pub = d.PublicCopy();
        Assert.Equal("web-1", pub.Config["vm_name"]);
    }

    [Fact]
    public async Task Rest_Config_RoundTrips_Through_Create_Get()
    {
        await using var h = await Harness.StartAsync();
        using var client = h.Client();

        var create = await client.PostRawAsync("/api/graphical-targets",
            "{\"protocol\":\"memory\",\"width\":16,\"height\":16," +
            "\"config\":{\"vm_name\":\"web-1\",\"replicas\":3,\"tls\":true}}");
        Assert.Equal(HttpStatusCode.Created, create.StatusCode);
        var created = await ReadJson(create);
        var id = created.GetProperty("target_id").GetString()!;

        // config present in the create response and not stripped.
        var cfgCreate = created.GetProperty("config");
        Assert.Equal("web-1", cfgCreate.GetProperty("vm_name").GetString());
        Assert.Equal(3, cfgCreate.GetProperty("replicas").GetInt32());
        Assert.True(cfgCreate.GetProperty("tls").GetBoolean());

        // config present again on GET.
        var get = await client.GetAsync($"/api/graphical-targets/{id}");
        var gotCfg = (await ReadJson(get)).GetProperty("config");
        Assert.Equal("web-1", gotCfg.GetProperty("vm_name").GetString());
    }

    [Fact]
    public async Task Rest_Config_NonObject_Is_422()
    {
        await using var h = await Harness.StartAsync();
        using var client = h.Client();
        Assert.Equal(422, (int)(await client.PostRawAsync("/api/graphical-targets",
            "{\"protocol\":\"memory\",\"width\":4,\"height\":4,\"config\":\"nope\"}")).StatusCode);
    }

    [Fact]
    public async Task Rest_Litevirt_Create_And_Validation()
    {
        await using var h = await Harness.StartAsync();
        using var client = h.Client();

        // litevirt with an endpoint → 201.
        var ok = await client.PostAsync("/api/graphical-targets",
            Json(new { protocol = "litevirt", endpoint = "10.0.0.5:7443", width = 8, height = 8 }));
        Assert.Equal(HttpStatusCode.Created, ok.StatusCode);
        Assert.Equal("litevirt", (await ReadJson(ok)).GetProperty("protocol").GetString());

        // litevirt without an endpoint → 422.
        Assert.Equal(422, (int)(await client.PostAsync("/api/graphical-targets",
            Json(new { protocol = "litevirt", width = 8, height = 8 }))).StatusCode);
    }

    [Fact]
    public async Task Registry_Update_Invalid_Definition()
    {
        var reg = new InMemoryGraphicalTargetRegistry();
        Assert.True(GraphicalTargetScope.TryForTenant(Tenant, out var scope));
        await reg.CreateAsync(scope, new Def { TargetId = "gt-u", TenantId = Tenant, Protocol = "memory", Width = 10, Height = 10 });

        var ex = await Assert.ThrowsAsync<GraphicalTargetException>(async () => await reg.UpdateAsync(scope, new Def { TargetId = "gt-u", TenantId = Tenant, Protocol = "memory", Width = 0, Height = 10 }));
        Assert.Equal(GraphicalTargetErrorCode.Invalid, ex.Code);
    }

    // ---- helpers ---------------------------------------------------------

    private static HttpContent Json(object o) =>
        new StringContent(JsonSerializer.Serialize(o), System.Text.Encoding.UTF8, "application/json");

    private static async Task<JsonElement> ReadJson(HttpResponseMessage resp)
    {
        var s = await resp.Content.ReadAsStringAsync();
        using var doc = JsonDocument.Parse(s);
        return doc.RootElement.Clone();
    }

    private sealed class Harness : IAsyncDisposable
    {
        private readonly UtermServer _server;
        private readonly string _baseUrl;
        private readonly string _token;

        private Harness(UtermServer server, string baseUrl, string token)
        {
            _server = server;
            _baseUrl = baseUrl;
            _token = token;
        }

        public HttpClient Client()
        {
            var c = new HttpClient { BaseAddress = new Uri(_baseUrl) };
            c.DefaultRequestHeaders.Add("Authorization", "Bearer " + _token);
            return c;
        }

        public static async Task<Harness> StartAsync(
            string? tenant = Tenant,
            string[]? roles = null,
            InMemoryGraphicalTargetRegistry? graphicalTargets = null)
        {
            var port = FreePort();
            var cfg = UtermServerConfig.Default();
            cfg.Server.Host = "127.0.0.1";
            cfg.Server.Port = port;
            cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
            cfg.Auth.Mode = "dev_token";

            var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
            {
                TokenPath = Path.Combine(Path.GetTempPath(), "uterm-gt-token-" + Guid.NewGuid().ToString("N")),
                Subject = "dev-user",
                Roles = roles ?? new[] { "admin" },
                Tenant = tenant,
            });

            var apiKeys = new ApiKeyStore();
            var auth = new LocalIdentityProvider(cfg.Auth, apiKeys);
            var authz = new AuthorizationService();
            var clock = new RealClock();
            var hub = new TermHub(new TermHubConfig { Clock = clock, WorkerToken = cfg.Auth.WorkerBearerToken });
            var registry = new InMemorySessionRegistry(cfg.Sessions);
            var server = new UtermServer(new ServerDeps
            {
                Hub = hub,
                Auth = auth,
                Authz = authz,
                Config = cfg,
                Registry = registry,
                GraphicalTargets = graphicalTargets ?? new InMemoryGraphicalTargetRegistry(),
                Version = "test",
                Clock = clock,
            });
            server.Build(new[] { $"http://127.0.0.1:{port}" });
            await server.StartAsync();
            return new Harness(server, $"http://127.0.0.1:{port}", token);
        }

        public ValueTask DisposeAsync() => _server.DisposeAsync();
    }

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }
}

internal static class HttpClientRawExtensions
{
    public static Task<HttpResponseMessage> PostRawAsync(
        this HttpClient c, string path, string json, HttpMethod? method = null)
    {
        var req = new HttpRequestMessage(method ?? HttpMethod.Post, path)
        {
            Content = new StringContent(json, System.Text.Encoding.UTF8, "application/json"),
        };
        return c.SendAsync(req);
    }
}
