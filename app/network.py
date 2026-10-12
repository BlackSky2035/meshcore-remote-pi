"""Netzkarte: aus Kontaktpfaden, gemeldeten Nachbarlisten und tatsächlich beobachteten Nachrichtenwegen
einen Graphen bauen.

Knoten: „me“ (dieser Node), bekannte Kontakte, und „unbekannte“ Zwischenstationen (nur Kennung bekannt).
Kanten: `path` (vom Node selbst gemeldeter Funkweg zu einem Kontakt, Reihenfolge von „me“ aus), `neighbour`
(ein Repeater hat den anderen gehört, mit SNR), `flood` (Kontakt bekannt, aber noch kein Pfad gelernt) und
`heard` (Repeater-Kette, über die tatsächlich eine Kanalnachricht ankam - ergänzt `path`, wenn der Node
selbst noch keinen Pfad zu einem Absender kennt; Direktnachrichten speichern diesen Weg bisher nicht).
Zwischenstationen im Pfad sind nur die ersten Bytes des Public Keys: passt genau ein Repeater/Room, wird er benannt, sonst bleibt es
eine Kennung (mehrdeutig → „?“). Keine Ortsdaten nötig. An echter Hardware nicht getestet (Pfad-Kennung > 1 Byte wird nicht unterstützt).
"""
from __future__ import annotations


def _resolve(prefix: str, contacts: list[dict]) -> list[dict]:
    p = prefix.lower()
    return [c for c in contacts if c["public_key"].lower().startswith(p)]


def _split_path(path: str, hash_size: int | None) -> list[str]:
    """Hex-Pfad in Hop-Kennungen zerlegen (gleiche Annahme wie beim Anzeigen im Kanal-Chat)."""
    step = max(1, hash_size or 1) * 2
    return [path[i:i + step] for i in range(0, len(path) - len(path) % step, step)]


def build_graph(contacts: list[dict], reports: list[dict], heard: list[dict] | None = None) -> dict:
    nodes: dict[str, dict] = {"me": {"id": "me", "name": "Dieser Node", "kind": "me"}}
    edges: dict[tuple, dict] = {}
    infra = [c for c in contacts if c.get("type_name") in ("repeater", "room")]

    def node_for(prefix: str) -> str:
        hits = _resolve(prefix, infra)
        if len(hits) == 1:
            c = hits[0]
            nodes.setdefault(c["public_key"], {"id": c["public_key"], "name": c["name"], "kind": c["type_name"],
                                               "lat": c.get("lat"), "lon": c.get("lon")})
            return c["public_key"]
        uid = f"?{prefix.lower()}"
        nodes.setdefault(uid, {"id": uid, "name": f"? {prefix}" + (" (mehrdeutig)" if len(hits) > 1 else ""), "kind": "unknown"})
        return uid

    def known_id(prefix: str) -> str | None:
        """Wie `node_for()`, aber ohne zu registrieren: None, wenn die Kennung zu keinem genau einen
        Kontakt passt. Für `heard`-Kanten, damit nicht jede nicht auflösbare Zwischenstation einen
        eigenen „?“-Punkt im Graphen erzeugt (bei vielen Nachrichten sonst schnell unübersichtlich)."""
        hits = _resolve(prefix, infra)
        return hits[0]["public_key"] if len(hits) == 1 else None

    def edge(a: str, b: str, kind: str, snr=None, via: str | None = None) -> None:
        if a == b:
            return
        key = (kind,) + tuple(sorted((a, b)))
        e = edges.setdefault(key, {"a": a, "b": b, "kind": kind, "snr": snr, "via": []})
        if snr is not None:
            e["snr"] = snr
        if via and via not in e["via"]:
            e["via"].append(via)

    routes: list[dict] = []
    for c in contacts:
        nodes.setdefault(c["public_key"], {"id": c["public_key"], "name": c["name"], "kind": c.get("type_name", "unknown"),
                                           "lat": c.get("lat"), "lon": c.get("lon")})
        plen = c.get("path_len")
        if c.get("type_name") == "client" or plen is not None:
            routes.append({"public_key": c["public_key"], "name": c["name"], "kind": c.get("type_name"), "path_len": plen, "hops": []})
        if plen == 0:
            edge("me", c["public_key"], "path", via=c["name"])
        elif isinstance(plen, int) and plen > 0 and c.get("path"):
            chain = ["me"] + [node_for(h) for h in c["path"]] + [c["public_key"]]
            routes[-1]["hops"] = [nodes[i]["name"] for i in chain[1:-1]]
            for a, b in zip(chain, chain[1:]):
                edge(a, b, "path", via=c["name"])
        elif plen == -1:
            # Flood: noch kein bekannter Pfad. Trotzdem eine (eigene, blasse) Linie zeigen, statt den
            # Kontakt unverbunden im Graphen schweben zu lassen - sonst sieht ein frisch hinzugefügter
            # Mesh ohne gelernte Pfade komplett unverbunden aus, obwohl die Kontakte durchaus da sind.
            edge("me", c["public_key"], "flood", via=c["name"])
    for r in reports:
        a = r["repeater"]
        if a not in nodes:
            continue
        hits = _resolve(r["prefix"], contacts)
        b = hits[0]["public_key"] if len(hits) == 1 else f"?{r['prefix'][:12].lower()}"
        if b.startswith("?"):
            nodes.setdefault(b, {"id": b, "name": f"? {r['prefix'][:12]}", "kind": "unknown"})
        elif b not in nodes:
            nodes[b] = {"id": b, "name": hits[0]["name"], "kind": hits[0].get("type_name", "unknown"),
                        "lat": hits[0].get("lat"), "lon": hits[0].get("lon")}
        edge(a, b, "neighbour", snr=r.get("snr"))
    for h in heard or []:
        hops = _split_path(h.get("path") or "", h.get("path_hash_size"))
        if not hops:
            continue
        # Reihenfolge wie gehört: fern -> nah -> me. Unbekannte Zwischenstationen werden NICHT registriert
        # (kein eigener „?“-Punkt) und auch nicht überbrückt - eine Linie nur zwischen zwei Stationen, die
        # im Pfad wirklich unmittelbar aufeinander folgten und beide bekannt sind.
        chain = [node_for(hp) if known_id(hp) else None for hp in hops] + ["me"]
        for a, b in zip(chain, chain[1:]):
            if a is None or b is None:
                continue
            edge(a, b, "heard")
    return {"nodes": list(nodes.values()), "edges": list(edges.values()), "routes": routes}
