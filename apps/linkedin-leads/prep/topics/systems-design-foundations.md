# Systems Design Foundations

Source: migrated from `~/Desktop/systems-design-practice/to-study.md`

## Purpose

This topic packet captures the systems design areas that should repeatedly show up in interview prep, flashcard generation, and technical briefing packets.

## Primary Topics

- caching with Redis and when cache layers are warranted
- load balancers and algorithm selection
- CDN integration and how edge delivery fits the overall architecture
- workers versus web services
- database sharding and partitioning
- read/write database separation and when it is worth the cost
- CAP theorem and distributed-systems tradeoffs
- SQL vs. NoSQL decisions
- indexing strategy
- differentiating web servers, application servers, and database servers

## Study Goals

- be able to discuss each topic breadth-first before diving deep
- explain cost tradeoffs instead of proposing architecture in a vacuum
- attach each concept to a concrete use case instead of giving dictionary definitions
- recognize when complexity is justified and when it is premature

## Flashcard Candidates

- round robin vs. least connections vs. IP hash
- what a CDN does and what it does not do
- how workers differ from request-serving web processes
- when sharding is appropriate
- when read replicas are useful
- where CAP theorem matters in practical design discussions

## Next Migration Steps

- break this packet into machine-friendly `PrepArtifact` records
- generate flashcards from the referenced interview video
- tag topics by interview stage, such as `system_design`, `backend`, and `distributed_systems`
