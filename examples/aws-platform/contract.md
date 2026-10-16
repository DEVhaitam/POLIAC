Monthly active users grew from about 5,000 to 42,000 over the last six months (roughly 8x) and
nothing in the infrastructure has changed since it was first provisioned. We're now getting
user-facing complaints across the board: the app feels sluggish under peak load, search results
are slow, sessions occasionally drop, and the nightly backup restore drill has become something
the on-call team dreads.

Priority is fixing the bottlenecks the data actually supports — this is not a cost-cutting pass,
we expect to keep growing. But don't recommend a change bigger than what the growth trend
justifies, and don't touch anything the data doesn't point at.
