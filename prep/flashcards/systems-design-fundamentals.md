# Systems Design Fundamentals Flashcards

Source: synthesized from `prep/topics/systems-design-fundamentals-study-guide.md`

## Flashcards

- Q: When is a cache warranted?
  A: When repeated expensive reads justify the added memory, invalidation, and stale-read complexity.

- Q: What does round robin optimize for?
  A: Simple even distribution when requests are roughly similar.

- Q: When is least-connections preferable to round robin?
  A: When request durations vary enough that active connection count is a better proxy for load.

- Q: What is a CDN good for?
  A: Serving cacheable content closer to users to reduce latency and origin load.

- Q: What is a CDN not a replacement for?
  A: Application logic and durable data storage.

- Q: What is the main difference between workers and web services?
  A: Web services handle request/response paths; workers process asynchronous background jobs.

- Q: When are read replicas useful?
  A: When read traffic dominates and some replication lag is acceptable.

- Q: Why are shards not just load balancers for databases?
  A: Because sharding changes how data is distributed and queried, not just how traffic is routed.

- Q: What is the practical interview use of CAP theorem?
  A: Explaining which tradeoff matters when network partitions occur in a distributed system.

- Q: What is the main cost of adding indexes?
  A: More storage and slower writes in exchange for faster reads.

- Q: How should SQL vs. NoSQL usually be justified?
  A: By access patterns, consistency requirements, and schema/query needs rather than fashion.

- Q: What is the safest order for explaining a system in an interview?
  A: Baseline architecture first, then bottlenecks, then scaling layers, then tradeoffs.
