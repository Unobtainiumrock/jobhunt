# Systems Design Cadence

Source: operationalized from `~/Desktop/systems-design-practice/to-study.md`

## Purpose

This cadence turns systems design prep into a repeatable weekly habit instead of a vague backlog item. It is intended to run continuously and intensify automatically when an active interview loop includes a technical or systems-design stage.

## Baseline Weekly Rhythm

- Session 1: breadth-first fundamentals review
  - focus on one or two core topics such as caching, load balancing, CAP theorem, or partitioning
  - goal: explain the concept, where it fits, and when it is justified
- Session 2: applied architecture drill
  - pick a product/system and walk through traffic flow, bottlenecks, tradeoffs, and cost implications
  - goal: practice speaking in a structured breadth-first then depth-second way
- Session 3: artifact reinforcement
  - convert weak areas into flashcards, short notes, or a debrief
  - goal: keep mistakes and insights from disappearing after one session

## Escalation When Interviewing

- If an `InterviewLoop` has a `system_design` stage:
  - add one extra focused prep block before the interview
  - review one company-specific dossier plus one reusable systems-design topic packet
  - rehearse one end-to-end design prompt out loud
- If an interview is scheduled within 48 hours:
  - stop opening new topics
  - switch to consolidation: flashcards, one architecture walkthrough, and one debrief review

## Session Template

1. Choose one primary topic.
2. Define the user/problem context before naming infrastructure.
3. Explain the baseline design first.
4. Add scaling layers only when a concrete bottleneck appears.
5. Call out cost and operational tradeoffs.
6. End with two or three flashcards or written takeaways.

## Current Topic Rotation

- caching and Redis
- load balancers and algorithm choice
- CDN usage and edge architecture
- workers versus request-serving web services
- sharding, partitioning, and read replicas
- CAP theorem and distributed-systems tradeoffs
- SQL versus NoSQL and indexing
- web server versus application server versus database server

## Integration With The Unified Hunt System

- stage-aware `Task` generation should point to this cadence when no interview is active
- recruiter-screen and system-design prep packets should attach the most relevant topic packet from `prep/topics/`
- post-interview debriefs should feed new flashcards and adjust the next week's focus
