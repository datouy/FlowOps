import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

export const useUserStore = defineStore('user', () => {
  const session = ref({
    user: '',
    role: '',
    permissions: [],
    scope: [],
    scopeMode: 'none',
    authRequired: true,
    localMode: false,
    reviewMode: 'cutter_required'
  })

  const loaded = ref(false)
  const isLoggedIn = computed(() => loaded.value && Boolean(session.value.user))
  const permissions = computed(() => new Set(session.value.permissions || []))

  const setSession = (info) => {
    session.value = {
      ...session.value,
      ...(info || {}),
      permissions: Array.isArray(info?.permissions) ? info.permissions : [],
      scope: Array.isArray(info?.scope) ? info.scope : []
    }
    loaded.value = true
  }

  const can = (permission) => permissions.value.has(String(permission || ''))
  const canAny = (...required) => required.some(permission => can(permission))
  const reset = () => {
    session.value = {
      user: '',
      role: '',
      permissions: [],
      scope: [],
      scopeMode: 'none',
      authRequired: true,
      localMode: false,
      reviewMode: 'cutter_required'
    }
    loaded.value = false
  }

  return {
    session,
    loaded,
    isLoggedIn,
    permissions,
    setSession,
    can,
    canAny,
    reset
  }
})
