<template>
  <section class="about">
    <header class="about-heading">
      <div class="product-mark" aria-hidden="true">云</div>
      <div>
        <h2>云策</h2>
        <p>在一个工作区完成内容、审核、发布和回复。</p>
      </div>
    </header>

    <dl class="about-facts">
      <div>
        <dt>平台</dt>
        <dd>抖音、小红书、视频号、快手、微博、百家号</dd>
      </div>
      <div>
        <dt>版本</dt>
        <dd>2.0</dd>
      </div>
    </dl>
  </section>
</template>

<style scoped lang="scss">
@use '@/styles/variables.scss' as *;

.about { max-width: 680px; padding: 28px; border: 1px solid $border-lighter; border-radius: 16px; background: $paper; }
.about-heading { display: flex; align-items: flex-start; gap: 16px; }
.product-mark { display: grid; place-items: center; flex: none; width: 44px; height: 44px; color: #fffaf5; border-radius: 10px; background: $primary-color; font-size: 18px; }
.about-heading h2 { color: $text-primary; font-size: 20px; font-weight: 500; }
.about-heading p { margin-top: 6px; color: $text-secondary; font-size: 13px; line-height: 1.65; }
.about-facts { display: grid; gap: 0; margin-top: 28px; border-top: 1px solid $border-lighter; }
.about-facts div { display: grid; grid-template-columns: 72px minmax(0, 1fr); gap: 16px; padding: 14px 0; border-bottom: 1px solid $border-lighter; }
.about-facts dt { color: $text-placeholder; font-size: 12px; }
.about-facts dd { color: $text-regular; font-size: 13px; line-height: 1.6; }
@media (max-width: 720px) { .about { padding: 20px; } }
</style>
