# Systems Design Video Flashcards

Source: interview-practice video referenced in `~/Desktop/systems-design-practice/to-study.md`

## Flashcards

- Q: What is the safest opening move in a systems-design interview?
  A: Start with the baseline architecture and clarify the core product requirements before adding scale layers.

- Q: What is the first backend shape for a simple web product?
  A: A web server or application service backed by a single database, with complexity added only when bottlenecks appear.

- Q: When should a load balancer enter the design?
  A: When one application instance is no longer enough for throughput or availability and traffic needs to be distributed across multiple instances.

- Q: What is the main tradeoff when proposing read replicas?
  A: They improve read scalability but add cost, operational complexity, and possible replication lag.

- Q: What interview mistake happens when adding sharding too early?
  A: Introducing major operational complexity before the system has clear evidence that a single database is the bottleneck.

- Q: What is one simple explanation for a CDN in an interview?
  A: It moves cacheable content closer to users to reduce latency and take pressure off the origin.

- Q: What is the difference between a worker and the main web service?
  A: The web service handles latency-sensitive request/response traffic, while workers process asynchronous background jobs.

- Q: How should caching usually be justified?
  A: By pointing to hot, repeated, expensive reads rather than naming Redis by reflex.

- Q: What question should you ask before proposing expensive multi-database infrastructure?
  A: What the scale, latency, reliability, and cost requirements actually are.

- Q: How should you talk about SQL vs. NoSQL in an interview?
  A: Tie the choice to access patterns, consistency needs, and operational tradeoffs instead of presenting one as universally better.

- Q: What is a good pattern for deeper follow-up after giving the baseline?
  A: Pick the most likely bottleneck, explain why it fails first, then show the next architectural step and its tradeoffs.

- Q: What communication habit makes a systems-design answer feel strong?
  A: Breadth-first explanation first, then one or two layers deeper where the design is actually under pressure.
