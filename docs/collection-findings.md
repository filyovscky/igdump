# Collection diagnostics — @oddnesse, 9 October 2026

## Incomplete lists of users who liked posts

The saved scan contains 71 posts. Five like lists are complete by the reported
counter and 66 are partial: 5,793 post/user relations versus 8,919 reported likes.
These are per-post relations, not the number of distinct people across the account.

Two fresh, authenticated requests to `/api/v1/media/{media_id}/likers/`
returned HTTP 200 with `status=ok`:

| Post | `user_count` | Returned users | Distinct returned users |
| --- | ---: | ---: | ---: |
| BV-eOdmHOu- | 78 | 74 | 74 |
| BoU3ey_hOjW | 312 | 97 | 97 |

Both responses have exactly these top-level fields: `users`, `user_count`,
`follow_ranking_token`, `status`. Neither provides `next_max_id`, a has-more
flag, identities of omitted users, or a reason for omission. There are no
duplicate users in either response. The normal Instagram Chrome likes dialog
for BV-eOdmHOu- calls the same endpoint and receives the same 74/78 response.
The smaller list is already incomplete before our parser processes it.

In the saved scan, 42 posts report more than 100 likes. The largest lists
report 215–312 likes but yield 95–100 users. This pattern is consistent with
an approximately 100-account cap on this endpoint, followed by further
visibility/filtering differences. It does not prove a universal current cap
or explain which four accounts are omitted from the 78-like post.

An upstream [instagram-private-api issue](https://github.com/dilame/instagram-private-api/issues/1497)
records the same endpoint returning 100 users with `user_count=127` in 2021.
This is historical supporting evidence, not a current Instagram guarantee.
Hiding public like counts is a separate feature; it does not establish why
these particular accessible lists are shorter.

No reliable cursor can be invented from the count. Repeating the same request
has already failed to add users here. Deleted, blocked, restricted or otherwise
unavailable accounts remain hypotheses; the responses do not identify them.
A future alternative collection method needs comparison of the actual user IDs
and explicit coverage, rather than treating the displayed counter as retrieved people.

## Performance of the first full scan

The scan's diagnostic log spans approximately **1,082.6 seconds (18 minutes)**.
It covers 71 posts, media, comments and likes, rather than just timeline links.

| Recorded HTTP category | Requests | Cumulative request duration |
| --- | ---: | ---: |
| Media downloads | 179 | 61.4 s |
| Comments | 71 | 38.2 s |
| Lists of users who liked posts | 137 | 176.4 s |

The 137 like requests include 71 initial requests and 66 automatic rechecks
of partial lists. Request durations sum to about 4.6 minutes. The remaining
wall time includes browser navigation/rendering, intentional request delays,
HTML/cache checkpoints and work not instrumented as Requests HTTP traffic.
Browser timing has not been measured separately, so the remainder is not all sleep.

At the default two-second delay, 137 like requests and 71 comment requests
account for approximately **416 seconds of intentional waiting**, with jitter.
The 66 additional like rechecks add roughly 132 seconds of waiting plus their
network time. These pauses and rechecks were not silently disabled.

The architecture can be improved. The most useful next changes are:

1. Reuse complete media payloads already received during timeline collection,
   opening individual post pages only when necessary fields are missing.
2. Make repeated checks of identical, cursor-less lists explicitly configurable,
   while preserving the original collection policy by default.
3. Checkpoint the cache after each post but render the entire HTML archive less
   often, retaining a final render and recovery behavior.
4. Consider small-scale concurrency for media files after profiling; keep API
   request pacing and stop-on-limit behavior intact.

The updated charts and maps rebuild from existing data offline. Changing a
chart time zone requires no Instagram requests. Street tiles are loaded only
when the user clicks the OpenStreetMap button.
