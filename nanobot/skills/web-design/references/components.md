# Copy-paste UI blocks (Tailwind CDN)

Load Tailwind in `<head>`:
```html
<script src="https://cdn.tailwindcss.com"></script>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
```

## Navbar (sticky, blur)
```html
<header class="sticky top-0 z-50 backdrop-blur bg-white/80 border-b border-slate-200">
  <nav class="max-w-6xl mx-auto px-6 h-16 flex items-center justify-between">
    <a href="#" class="text-xl font-extrabold tracking-tight text-slate-900">Acme</a>
    <div class="hidden md:flex gap-8 text-sm font-medium text-slate-600">
      <a href="#features" class="hover:text-slate-900">Features</a>
      <a href="#pricing" class="hover:text-slate-900">Pricing</a>
      <a href="#about" class="hover:text-slate-900">About</a>
    </div>
    <a href="#cta" class="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow hover:bg-indigo-700 transition">Get started</a>
  </nav>
</header>
```

## Hero (centered, bold)
```html
<section class="relative overflow-hidden bg-gradient-to-b from-indigo-50 to-white">
  <div class="max-w-6xl mx-auto px-6 pt-24 pb-20 text-center">
    <span class="inline-block rounded-full bg-indigo-100 px-3 py-1 text-xs font-semibold uppercase tracking-wide text-indigo-700">New · v2.0</span>
    <h1 class="mt-6 text-5xl md:text-6xl font-extrabold tracking-tight text-slate-900">Build faster with a<br><span class="text-indigo-600">beautiful foundation</span></h1>
    <p class="mx-auto mt-6 max-w-2xl text-lg text-slate-600">Ship production-ready web apps in hours, not weeks. Clean code, modern design, zero config.</p>
    <div class="mt-8 flex justify-center gap-4">
      <a href="#cta" class="rounded-xl bg-indigo-600 px-6 py-3 font-semibold text-white shadow-lg hover:-translate-y-0.5 transition">Start free</a>
      <a href="#features" class="rounded-xl border border-slate-300 px-6 py-3 font-semibold text-slate-700 hover:bg-slate-50 transition">See how it works</a>
    </div>
  </div>
</section>
```

## Features grid
```html
<section id="features" class="py-20 bg-white">
  <div class="max-w-6xl mx-auto px-6">
    <h2 class="text-3xl font-bold text-center text-slate-900">Everything you need</h2>
    <div class="mt-12 grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
      <!-- repeat card -->
      <div class="rounded-2xl border border-slate-200 p-6 shadow-sm hover:shadow-md transition">
        <div class="flex h-11 w-11 items-center justify-center rounded-xl bg-indigo-100 text-indigo-600 text-xl">⚡</div>
        <h3 class="mt-4 text-lg font-semibold text-slate-900">Fast by default</h3>
        <p class="mt-2 text-sm text-slate-600">Optimized assets and lazy loading out of the box.</p>
      </div>
    </div>
  </div>
</section>
```

## Pricing
```html
<section id="pricing" class="py-20 bg-slate-50">
  <div class="max-w-6xl mx-auto px-6 grid gap-6 md:grid-cols-3">
    <div class="rounded-2xl border border-slate-200 bg-white p-8">
      <h3 class="text-lg font-semibold text-slate-900">Starter</h3>
      <p class="mt-2 text-4xl font-extrabold text-slate-900">$0<span class="text-base font-medium text-slate-500">/mo</span></p>
      <ul class="mt-6 space-y-3 text-sm text-slate-600"><li>✓ 1 project</li><li>✓ Community support</li></ul>
      <a href="#" class="mt-8 block rounded-xl border border-slate-300 py-3 text-center font-semibold text-slate-700 hover:bg-slate-50">Choose</a>
    </div>
    <div class="rounded-2xl border-2 border-indigo-600 bg-white p-8 shadow-lg relative">
      <span class="absolute -top-3 left-1/2 -translate-x-1/2 rounded-full bg-indigo-600 px-3 py-1 text-xs font-semibold text-white">Most popular</span>
      <h3 class="text-lg font-semibold text-slate-900">Pro</h3>
      <p class="mt-2 text-4xl font-extrabold text-slate-900">$24<span class="text-base font-medium text-slate-500">/mo</span></p>
      <ul class="mt-6 space-y-3 text-sm text-slate-600"><li>✓ Unlimited projects</li><li>✓ Priority support</li><li>✓ Custom domains</li></ul>
      <a href="#" class="mt-8 block rounded-xl bg-indigo-600 py-3 text-center font-semibold text-white hover:bg-indigo-700">Choose</a>
    </div>
    <div class="rounded-2xl border border-slate-200 bg-white p-8">
      <h3 class="text-lg font-semibold text-slate-900">Team</h3>
      <p class="mt-2 text-4xl font-extrabold text-slate-900">$99<span class="text-base font-medium text-slate-500">/mo</span></p>
      <ul class="mt-6 space-y-3 text-sm text-slate-600"><li>✓ SSO</li><li>✓ Audit logs</li></ul>
      <a href="#" class="mt-8 block rounded-xl border border-slate-300 py-3 text-center font-semibold text-slate-700 hover:bg-slate-50">Contact</a>
    </div>
  </div>
</section>
```

## Testimonials
```html
<section class="py-20 bg-white">
  <div class="max-w-6xl mx-auto px-6 grid gap-6 md:grid-cols-2">
    <figure class="rounded-2xl bg-slate-50 p-8 border border-slate-200">
      <blockquote class="text-slate-700 italic">“The cleanest starter I've used. Saved me a full week.”</blockquote>
      <figcaption class="mt-4 flex items-center gap-3">
        <img class="h-10 w-10 rounded-full" src="https://i.pravatar.cc/80?img=12" alt="">
        <div><p class="font-semibold text-slate-900">Jordan Lee</p><p class="text-sm text-slate-500">CTO, Northwind</p></div>
      </figcaption>
    </figure>
  </div>
</section>
```

## CTA band
```html
<section id="cta" class="py-20 bg-indigo-600">
  <div class="max-w-4xl mx-auto px-6 text-center">
    <h2 class="text-3xl md:text-4xl font-bold text-white">Ready to build something great?</h2>
    <a href="#" class="mt-8 inline-block rounded-xl bg-white px-8 py-4 font-semibold text-indigo-700 shadow-lg hover:-translate-y-0.5 transition">Get started free</a>
  </div>
</section>
```

## Footer
```html
<footer class="bg-slate-900 text-slate-400">
  <div class="max-w-6xl mx-auto px-6 py-14 grid gap-10 sm:grid-cols-2 lg:grid-cols-4">
    <div><p class="text-white font-bold text-lg">Acme</p><p class="mt-3 text-sm">Beautiful foundations for fast teams.</p></div>
    <div><p class="text-white font-semibold mb-3">Product</p><ul class="space-y-2 text-sm"><li><a href="#" class="hover:text-white">Features</a></li><li><a href="#" class="hover:text-white">Pricing</a></li></ul></div>
    <div><p class="text-white font-semibold mb-3">Company</p><ul class="space-y-2 text-sm"><li><a href="#" class="hover:text-white">About</a></li><li><a href="#" class="hover:text-white">Careers</a></li></ul></div>
    <div><p class="text-white font-semibold mb-3">Legal</p><ul class="space-y-2 text-sm"><li><a href="#" class="hover:text-white">Privacy</a></li><li><a href="#" class="hover:text-white">Terms</a></li></ul></div>
  </div>
  <div class="border-t border-slate-800 py-6 text-center text-sm">© 2026 Acme Inc.</div>
</footer>
```

## Form
```html
<form class="max-w-md mx-auto space-y-4">
  <label class="block text-sm font-medium text-slate-700">Email
    <input type="email" required placeholder="you@company.com"
      class="mt-1 block w-full rounded-lg border border-slate-300 px-4 py-2.5 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-200 outline-none">
  </label>
  <button class="w-full rounded-lg bg-indigo-600 py-3 font-semibold text-white hover:bg-indigo-700">Subscribe</button>
</form>
```