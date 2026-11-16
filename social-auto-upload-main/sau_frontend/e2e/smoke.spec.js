import { expect, test } from '@playwright/test'

test.beforeEach(async ({ page }) => {
  await page.route('http://127.0.0.1:5409/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/auth/check') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ code: 200, data: { authenticated: true }, msg: null })
      })
    }
    if (path === '/ai/status') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          code: 200,
          data: { configured: true, model: 'e2e-model', base_url: 'https://ai.example/v1' },
          msg: null
        })
      })
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ code: 200, data: [], msg: null, pagination: { page: 1, page_size: 50, total: 0, pages: 0, has_next: false } })
    })
  })
})

test('loads the local workspace and lazy route navigation', async ({ page }) => {
  await page.goto('/#/about')
  await expect(page.getByRole('heading', { name: '自媒体自动化运营系统' })).toBeVisible()

  await page.getByText('AI 文案', { exact: true }).click()
  await expect(page).toHaveURL(/#\/ai-content/)
  await expect(page.getByRole('heading', { name: 'AI 文案生成' })).toBeVisible()
  await expect(page.getByText('模型 e2e-model')).toBeVisible()
})
