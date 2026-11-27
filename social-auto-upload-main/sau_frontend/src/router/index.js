import { createRouter, createWebHashHistory } from 'vue-router'

const Today = () => import('../views/Today.vue')
const ContentCenter = () => import('../views/ContentCenter.vue')
const Inbox = () => import('../views/Inbox.vue')
const Insights = () => import('../views/Insights.vue')
const WorkspaceSettings = () => import('../views/WorkspaceSettings.vue')
const OfficialConsole = () => import('../views/OfficialConsole.vue')

const redirectWithView = (path, view) => to => ({
  path,
  query: { ...to.query, view }
})

const routes = [
  { path: '/', redirect: '/today' },
  { path: '/today', name: 'Today', component: Today },
  { path: '/content', name: 'Content', component: ContentCenter },
  { path: '/inbox', name: 'Inbox', component: Inbox },
  { path: '/console', name: 'OfficialConsole', component: OfficialConsole },
  { path: '/insights', name: 'Insights', component: Insights },
  { path: '/settings', name: 'Settings', component: WorkspaceSettings },

  // Compatibility routes: bookmarks and integrations keep working while the
  // old module pages become views inside the four task-oriented workspaces.
  { path: '/todo', redirect: '/today' },
  { path: '/messages', redirect: redirectWithView('/inbox', 'messages') },
  { path: '/comments', redirect: redirectWithView('/inbox', 'comments') },
  { path: '/publish-center', redirect: redirectWithView('/content', 'compose') },
  { path: '/content-pack', redirect: redirectWithView('/content', 'pack') },
  { path: '/ai-content', redirect: redirectWithView('/content', 'ai') },
  { path: '/review', redirect: redirectWithView('/content', 'review') },
  { path: '/publish-calendar', redirect: redirectWithView('/content', 'calendar') },
  { path: '/publish-records', redirect: redirectWithView('/insights', 'works') },
  { path: '/weekly-report', redirect: redirectWithView('/insights', 'overview') },
  { path: '/roi', redirect: redirectWithView('/insights', 'leads') },
  { path: '/account-management', redirect: redirectWithView('/settings', 'accounts') },
  { path: '/material-management', redirect: redirectWithView('/settings', 'materials') },
  { path: '/ops', redirect: redirectWithView('/settings', 'diagnostics') },
  { path: '/about', redirect: redirectWithView('/settings', 'about') },
  { path: '/:pathMatch(.*)*', redirect: '/today' }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

export default router
