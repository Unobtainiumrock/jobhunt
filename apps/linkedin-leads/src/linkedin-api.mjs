/**
 * LinkedIn Voyager MessagingGraphQL API helpers.
 *
 * LinkedIn moved messaging to a GraphQL-based endpoint at
 * /voyager/api/voyagerMessagingGraphQL/graphql. All calls run as fetch()
 * inside the authenticated browser context via CDP Runtime.evaluate.
 */

/**
 * Build a JS expression that calls the LinkedIn MessagingGraphQL endpoint.
 */
function messagingGraphQL(queryId, variables, csrfToken) {
  // LinkedIn uses a proprietary encoding in the variables param —
  // colons, parens, and commas are percent-encoded within the query string.
  const url = `/voyager/api/voyagerMessagingGraphQL/graphql?queryId=${queryId}&variables=${variables}`;
  return `
    (async () => {
      try {
        const resp = await fetch("${url}", {
          headers: {
            "csrf-token": "${csrfToken}",
            "accept": "application/graphql",
            "x-restli-protocol-version": "2.0.0",
          },
          credentials: "include",
        });
        if (!resp.ok) return JSON.stringify({ error: resp.status, url: "${url}" });
        return await resp.text();
      } catch (e) {
        return JSON.stringify({ error: e.message });
      }
    })()
  `;
}

function voyagerFetch(endpoint, csrfToken) {
  return `
    (async () => {
      try {
        const resp = await fetch("${endpoint}", {
          headers: {
            "csrf-token": "${csrfToken}",
            "accept": "application/vnd.linkedin.normalized+json+2.1",
            "x-restli-protocol-version": "2.0.0",
          },
          credentials: "include",
        });
        if (!resp.ok) return JSON.stringify({ error: resp.status });
        return await resp.text();
      } catch (e) {
        return JSON.stringify({ error: e.message });
      }
    })()
  `;
}

/**
 * LinkedIn messaging inbox categories. The UI splits these into separate tabs,
 * so hitting only PRIMARY_INBOX silently drops older InMails + message requests.
 */
export const INBOX_CATEGORIES = Object.freeze([
  'PRIMARY_INBOX',
  'INMAIL',
  'MESSAGE_REQUEST',
  'OTHER',
]);

/**
 * Fetch messaging conversations (inbox threads) with cursor-based pagination.
 * Uses the category-filtered endpoint that LinkedIn's UI actually uses.
 * @param {string} csrfToken
 * @param {string} mailboxUrn - Your fsd_profile URN (e.g. urn:li:fsd_profile:ACoAA...)
 * @param {string|null} nextCursor - Pagination cursor from previous response metadata
 * @param {string} category - One of INBOX_CATEGORIES; defaults to PRIMARY_INBOX
 * @param {number} count - Page size; LinkedIn accepts up to ~40
 */
export function fetchConversations(
  csrfToken,
  mailboxUrn,
  nextCursor = null,
  category = 'PRIMARY_INBOX',
  count = 20,
) {
  const encodedUrn = mailboxUrn.replace(/:/g, '%3A');
  let variables = `(query:(predicateUnions:List((conversationCategoryPredicate:(category:${category})))),count:${count},mailboxUrn:${encodedUrn}`;
  if (nextCursor) {
    variables += `,nextCursor:${encodeURIComponent(nextCursor)}`;
  }
  variables += ')';
  return messagingGraphQL(
    'messengerConversations.9501074288a12f3ae9e3c7ea243bccbf',
    variables,
    csrfToken,
  );
}

/**
 * Fetch messages within a specific conversation.
 * @param {string} csrfToken
 * @param {string} conversationUrn - Full msg_conversation URN
 */
export function fetchMessages(csrfToken, conversationUrn) {
  const encodedUrn = conversationUrn
    .replace(/:/g, '%3A')
    .replace(/\(/g, '%28')
    .replace(/\)/g, '%29')
    .replace(/,/g, '%2C')
    .replace(/=/g, '%3D');
  const variables = `(conversationUrn:${encodedUrn})`;
  return messagingGraphQL(
    'messengerMessages.5846eeb71c981f11e0134cb6626cc314',
    variables,
    csrfToken,
  );
}

/**
 * Fetch the current user's profile (for identifying self and getting mailbox URN).
 */
export function fetchCurrentProfile(csrfToken) {
  return voyagerFetch('/voyager/api/me', csrfToken);
}
