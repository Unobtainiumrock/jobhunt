# Systems Design Fundamentals Study Guide

Source: synthesized from `~/Desktop/systems-design-practice/to-study.md`

## Purpose

This guide converts the original loose study list into a practical review packet for interviews. The goal is not encyclopedic depth. The goal is being able to explain the baseline, the trigger for extra complexity, and the tradeoffs of each design move.

## Breadth-First Rules

- Start with the user-facing problem, not the infrastructure buzzwords.
- Give a simple baseline design before adding scale layers.
- Only introduce complexity when the bottleneck is explicit.
- Mention cost and operational burden when proposing replicas, shards, or multiple services.
- Go one or two layers deep on the chosen hotspot after covering the broad system clearly.

## Core Topics

### Caching and Redis

- Use caching when the same expensive reads happen often enough to justify memory cost and cache invalidation complexity.
- Good triggers:
  - hot read paths
  - expensive aggregations
  - expensive third-party fetches
- Be able to explain:
  - cache-aside versus write-through/write-back at a high level
  - cache invalidation risk
  - stale reads as a tradeoff

### Load Balancers

- Use a load balancer when multiple application instances should share traffic or when you need health checks and failover.
- Know the common algorithms:
  - round robin: simple even distribution
  - least connections: better when request durations vary
  - IP hash: sticky routing when affinity matters
- Key tradeoff:
  - they improve availability and distribution but add infrastructure and observability requirements

### CDN and Edge Delivery

- A CDN reduces latency and origin load for cacheable content by serving assets closer to users.
- Know what it does not replace:
  - it is not a database
  - it is not your application logic
  - it does not eliminate the need for origin capacity
- Useful interview angle:
  - describe where CDN caching ends and dynamic app/database work begins

### Workers Versus Web Services

- Web services handle request/response paths.
- Workers handle asynchronous jobs outside the latency-sensitive user path.
- Good examples for workers:
  - email sending
  - report generation
  - media processing
  - enrichment/classification jobs
- Tradeoff:
  - queues and workers add reliability and throughput, but increase operational moving parts

### Sharding and Partitioning

- Partitioning splits data for scale or manageability.
- Sharding is horizontal partitioning across multiple database instances.
- Good trigger:
  - single-node storage or throughput ceilings
- Risks:
  - cross-shard joins
  - hot partitions
  - rebalancing complexity
- Interview phrasing:
  - shards are not "load balancers for databases"; they change the data distribution model itself

### Read Replicas and Read/Write Separation

- Read replicas help when reads dominate writes and replication lag is acceptable.
- They are useful for:
  - reporting
  - feed reads
  - analytics-ish read pressure
- Important caveat:
  - extra databases cost money and operational complexity, so mention that before proposing them casually

### CAP Theorem and Distributed Systems

- In a distributed system under partition, you choose stronger consistency or stronger availability characteristics.
- Practical interview move:
  - map the choice to the product requirement rather than reciting the theorem in the abstract

### SQL Versus NoSQL

- SQL is often better when joins, relational integrity, and structured querying matter.
- NoSQL is often better when flexible schemas, high write scale, or document/key-value access patterns dominate.
- Good answer pattern:
  - choose based on access patterns, consistency needs, and operational familiarity

### Indexes

- Indexes speed reads but cost write performance and storage.
- Always tie indexing discussion to query patterns rather than treating indexes as free speed.

### Server Roles

- Web server: handles HTTP concerns and static assets
- Application server: runs business logic
- Database server: stores and retrieves data
- The exact boundary may blur in modern stacks, but the conceptual distinction still matters in interviews

## Interview Checklist

- What is the baseline architecture?
- Where is the expected bottleneck first?
- What metric forces the next scaling step?
- What does the new component cost operationally?
- What failure mode does it introduce?
- What simpler option was rejected and why?

## Immediate Follow-Ons

- turn weak spots into flashcards
- practice one spoken design walkthrough per week
- attach company-specific context when an active interview loop exists
