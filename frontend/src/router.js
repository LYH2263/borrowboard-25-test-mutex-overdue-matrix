import { createRouter, createWebHistory } from 'vue-router'
import Board from './pages/Board.vue'
import ListItem from './pages/ListItem.vue'
import Loans from './pages/Loans.vue'
import Owners from './pages/Owners.vue'
import Settings from './pages/Settings.vue'
export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: Board },
    { path: '/list', component: ListItem },
    { path: '/loans', component: Loans },
    { path: '/owners', component: Owners },
    { path: '/settings', component: Settings },
  ],
})
