import { createRouter, createWebHashHistory } from 'vue-router'

const Dashboard = () => import('../views/Dashboard.vue')
const AccountManagement = () => import('../views/AccountManagement.vue')
const MaterialManagement = () => import('../views/MaterialManagement.vue')
const PublishCenter = () => import('../views/PublishCenter.vue')
const About = () => import('../views/About.vue')
const PublishCalendar = () => import('../views/PublishCalendar.vue')
const PublishRecords = () => import('../views/PublishRecords.vue')
const AIContent = () => import('../views/AIContent.vue')
const Messages = () => import('../views/Messages.vue')
const Comments = () => import('../views/Comments.vue')
const Todo = () => import('../views/Todo.vue')
const ContentPack = () => import('../views/ContentPack.vue')
const Review = () => import('../views/Review.vue')
const WeeklyReport = () => import('../views/WeeklyReport.vue')
const Roi = () => import('../views/Roi.vue')
const OpsConsole = () => import('../views/OpsConsole.vue')

const routes = [
  {
    path: '/',
    name: 'Dashboard',
    component: Dashboard
  },
  {
    path: '/account-management',
    name: 'AccountManagement',
    component: AccountManagement
  },
  {
    path: '/material-management',
    name: 'MaterialManagement',
    component: MaterialManagement
  },
  {
    path: '/publish-center',
    name: 'PublishCenter',
    component: PublishCenter
  },
  {
    path: '/ai-content',
    name: 'AIContent',
    component: AIContent
  },
  {
    path: '/messages',
    name: 'Messages',
    component: Messages
  },
  {
    path: '/comments',
    name: 'Comments',
    component: Comments
  },
  {
    path: '/todo',
    name: 'Todo',
    component: Todo
  },
  {
    path: '/content-pack',
    name: 'ContentPack',
    component: ContentPack
  },
  {
    path: '/review',
    name: 'Review',
    component: Review
  },
  {
    path: '/weekly-report',
    name: 'WeeklyReport',
    component: WeeklyReport
  },
  {
    path: '/publish-records',
    name: 'PublishRecords',
    component: PublishRecords
  },
  {
    path: '/publish-calendar',
    name: 'PublishCalendar',
    component: PublishCalendar
  },
  {
    path: '/roi',
    name: 'Roi',
    component: Roi
  },
  {
    path: '/ops',
    name: 'OpsConsole',
    component: OpsConsole
  },
  {
    path: '/about',
    name: 'About',
    component: About
  }
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})

export default router
