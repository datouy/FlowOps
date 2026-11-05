import { http } from '@/utils/request'

// 评论运营：同步、列表、回复 / 点赞 / 删除 / 置顶
export const commentApi = {
  // 评论列表（附带各平台能力矩阵）
  list(params = {}) {
    return http.get('/comments', params)
  },

  // 各平台评论能力矩阵
  capabilities() {
    return http.get('/comments/capabilities')
  },

  // 同步状态
  status() {
    return http.get('/comments/status')
  },

  // 立即同步一轮
  sync() {
    return http.post('/comments/sync', {})
  },

  // 回复评论
  reply(id, text) {
    return http.post(`/comments/${id}/reply`, { text })
  },

  // 点赞 / 取消点赞
  like(id, liked = true) {
    return http.post(`/comments/${id}/like`, { liked })
  },

  // 删除评论
  remove(id) {
    return http.post(`/comments/${id}/delete`, {})
  },

  // 置顶 / 取消置顶
  pin(id, pinned = true) {
    return http.post(`/comments/${id}/pin`, { pinned })
  }
}

export default commentApi
