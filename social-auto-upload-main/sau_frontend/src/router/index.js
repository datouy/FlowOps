import { createRouter, createWebHashHistory } from 'vue-router'
import Dashboard from '../views/Dashboard.vue'
import AccountManagement from '../views/AccountManagement.vue'
import MaterialManagement from '../views/MaterialManagement.vue'
import PublishCenter from '../views/PublishCenter.vue'
import About from '../views/About.vue'
import PublishCalendar from '../views/PublishCalendar.vue'
import PublishRecords from '../views/PublishRecords.vue'
import AIContent from '../views/AIContent.vue'
import Messages from '../views/Messages.vue'
import Todo from '../views/Todo.vue'
import ContentPack from '../views/ContentPack.vue'
import Review from '../views/Review.vue'
import WeeklyReport from '../views/WeeklyReport.vue'

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
