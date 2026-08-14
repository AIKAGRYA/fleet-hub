# Operator punch list (John, 2026-08-11)

Priority: P0 = auth + dynamic presence + real status. P1 = mobile + broker health + notifications. P2 = topology + node health + trading + alerts. P3 = polish.

## Structural
1. No auth — open URL, anyone can read/send. Needs token gate.
2. Roster hardcoded in Python — agent status is static, not dynamic.
3. No mobile-first layout. Operator is on phone 90% of time.
4. Dark-only, no toggle.

## Data gaps
5. No broker health panel in UI (API has it, frontend doesn't show it).
6. Kanban: 10 stale tasks from July 23, read-only, no add/edit/drag. Dead board.
7. Agent "live/offline" never updates. Megha shows live but One-Pane down. Rushabdev shows live but SSH down.
8. No node health metrics (disk, mem, CPU, restart counts).
9. Chat limited to 30 msgs, no pagination, no search, no date filter.
10. No leafnode/topology view — most critical infra piece is invisible.

## UX
11. No notifications (sound, badge, browser). Miss messages if not looking.
12. No threading/replies — flat stream only.
13. Raw NATS feed: no filter, no search, 100 msg max.
14. Send box max 500 chars.
15. No relative timestamps, no UTC toggle.
16. No agent avatars or visual identity.
17. Self-Evolution page is static essay, not interactive.
18. No "last seen" timestamps on agents.

## Missing features
19. No trading lab integration (P&L, routes, positions).
20. No SAB/Moltbook integration (engagement, wiki, content pipeline).
21. No infrastructure topology map (3 nodes, services, dependencies).
22. No alert/incident panel (crash loops, node downs).
23. No operator action log / audit trail.
24. No A2A packet trace visualization.
25. No export/report (JSON/CSV download).
