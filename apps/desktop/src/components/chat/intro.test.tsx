import { cleanup, render } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { Intro } from './intro'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

it('uses the supplied seed instead of mount-time randomness', () => {
  vi.spyOn(Math, 'random').mockReturnValueOnce(0).mockReturnValueOnce(0.00001)

  const first = render(<Intro personality="none" seed={0} />).container.textContent
  cleanup()
  const second = render(<Intro personality="none" seed={0} />).container.textContent

  expect(second).toBe(first)
})
