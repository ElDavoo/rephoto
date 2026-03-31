import { test, expect } from '@playwright/test';

test('test', async ({ page }) => {
  await page.goto('https://www.google.com/photos/about/');
  await page.getByRole('link', { name: 'Download the Google Photos app' }).click();
  await page.getByRole('link', { name: 'Download the Google Photos app' }).click();
});