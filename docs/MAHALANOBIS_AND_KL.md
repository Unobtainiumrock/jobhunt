# Mahalanobis Distance and KL Divergence

This note captures the conceptual relationship that the older master's-program note was meant to preserve. A filesystem search did not recover the original note locally, so this document serves as the replacement summary.

## Core Relationship

- Mahalanobis distance measures how far a point is from the mean of a distribution after scaling by the covariance structure.
- KL divergence measures how different one probability distribution is from another in terms of expected information loss.

They are not the same object, but they meet in Gaussian settings.

## Where They Connect

For multivariate Gaussians, the KL divergence contains a quadratic term of the form:

`(mu2 - mu1)^T Sigma^{-1} (mu2 - mu1)`

That quadratic form is the squared Mahalanobis distance between the means under the chosen covariance metric.

So the practical connection is:

- Mahalanobis distance is the geometry term induced by a covariance matrix.
- KL divergence between Gaussians includes that geometry term plus extra terms for covariance mismatch and normalization.

## Intuition

- Euclidean distance treats every direction equally.
- Mahalanobis distance rescales directions according to variance and correlation.
- KL divergence goes one step further and asks how much one full distribution fails to match another.

That makes Mahalanobis distance feel like a local geometric ingredient, while KL divergence is the fuller distribution-level discrepancy.

## Why This Matters

This is useful when reasoning about:

- anomaly detection under correlated features
- Gaussian discriminant models
- metric learning with covariance-aware geometry
- retrieval or scoring systems where “distance” and “distribution mismatch” should not be conflated

## Short Takeaway

If the data are approximately Gaussian, squared Mahalanobis distance appears naturally inside the KL divergence expression. Mahalanobis tells you how far the means are apart in covariance-aware space; KL tells you how different the full distributions are.
