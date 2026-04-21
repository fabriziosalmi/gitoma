import { defineConfig } from 'vitepress'

// The repo is served at https://<user>.github.io/gitoma/ — so every asset
// and internal link needs the `/gitoma/` base prefix. Keep this in one
// place; the deploy workflow doesn't override it.
const base = '/gitoma/'

export default defineConfig({
  lang: 'en-US',
  title: 'Gitoma',
  description:
    'Autonomous GitHub agent that analyzes, plans, commits, and opens pull requests.',
  base,
  cleanUrls: true,
  lastUpdated: true,
  sitemap: { hostname: 'https://fabriziosalmi.github.io/gitoma/' },

  head: [
    ['link', { rel: 'icon', type: 'image/svg+xml', href: `${base}logo.svg` }],
    ['meta', { name: 'theme-color', content: '#0a0a0b' }],
    ['meta', { name: 'color-scheme', content: 'light dark' }],
    [
      'meta',
      {
        property: 'og:title',
        content: 'Gitoma — autonomous GitHub improvement agent',
      },
    ],
    [
      'meta',
      {
        property: 'og:description',
        content:
          'A local-first autonomous agent that improves your GitHub repositories end to end.',
      },
    ],
    ['meta', { property: 'og:type', content: 'website' }],
  ],

  themeConfig: {
    logo: { src: '/logo.svg', alt: 'Gitoma' },
    siteTitle: 'Gitoma',

    // ── Nav ───────────────────────────────────────────────────────────────
    nav: [
      { text: 'Guide', link: '/guide/quickstart', activeMatch: '/guide/' },
      { text: 'API', link: '/api/rest', activeMatch: '/api/' },
      {
        text: 'Architecture',
        link: '/architecture/overview',
        activeMatch: '/architecture/',
      },
      {
        text: 'v0.1',
        items: [
          {
            text: 'Changelog',
            link: 'https://github.com/fabriziosalmi/gitoma/releases',
          },
          {
            text: 'Contributing',
            link: '/architecture/contributing',
          },
          {
            text: 'License',
            link: 'https://github.com/fabriziosalmi/gitoma/blob/main/LICENSE',
          },
        ],
      },
    ],

    // ── Sidebar ───────────────────────────────────────────────────────────
    sidebar: {
      '/guide/': [
        {
          text: 'Getting started',
          items: [
            { text: 'Introduction', link: '/guide/' },
            { text: 'Prerequisites', link: '/guide/prerequisites' },
            { text: 'Install', link: '/guide/install' },
            { text: 'Quickstart', link: '/guide/quickstart' },
          ],
        },
        {
          text: 'Using Gitoma',
          items: [
            { text: 'CLI reference', link: '/guide/cli' },
            { text: 'Web cockpit', link: '/guide/cockpit' },
            { text: 'Configuration', link: '/guide/configuration' },
            { text: 'Observability', link: '/guide/observability' },
          ],
        },
      ],
      '/api/': [
        {
          text: 'API',
          items: [
            { text: 'REST endpoints', link: '/api/rest' },
            { text: 'Authentication', link: '/api/auth' },
            { text: 'Streaming (SSE)', link: '/api/streaming' },
            { text: 'MCP server', link: '/api/mcp' },
          ],
        },
      ],
      '/architecture/': [
        {
          text: 'Architecture',
          items: [
            { text: 'Overview', link: '/architecture/overview' },
            { text: 'Pipeline + state machine', link: '/architecture/pipeline' },
            { text: 'Security + threat model', link: '/architecture/security' },
            { text: 'Contributing', link: '/architecture/contributing' },
          ],
        },
      ],
    },

    socialLinks: [
      { icon: 'github', link: 'https://github.com/fabriziosalmi/gitoma' },
    ],

    footer: {
      message:
        'Released under the MIT License. Local-first by design. No telemetry.',
      copyright: `© ${new Date().getFullYear()} Fabrizio Salmi`,
    },

    outline: { level: [2, 3], label: 'On this page' },

    editLink: {
      pattern:
        'https://github.com/fabriziosalmi/gitoma/edit/main/docs/:path',
      text: 'Suggest an edit to this page',
    },

    search: {
      provider: 'local',
      options: {
        detailedView: true,
      },
    },

    docFooter: {
      prev: '← Previous',
      next: 'Next →',
    },
  },

  markdown: {
    theme: { light: 'github-light', dark: 'github-dark' },
    lineNumbers: false,
  },
})
