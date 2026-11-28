import { describe, expect, it } from 'vitest'
import { itemLabel, unitLabel } from './units'

describe('itemLabel', () => {
  it('pluralizes and singularizes counted items', () => {
    expect(itemLabel('egg', 2, '')).toBe('eggs')
    expect(itemLabel('egg', 1, '')).toBe('egg')
    expect(itemLabel('scallions', 1, '')).toBe('scallion')
    expect(itemLabel('scallions', 0.5, '')).toBe('scallion')
    expect(itemLabel('red onion', 1.5, '')).toBe('red onions')
    expect(itemLabel('tomato', 3, '')).toBe('tomatoes')
    expect(itemLabel('tomatoes', 1, '')).toBe('tomato')
    expect(itemLabel('bay leaf', 2, '')).toBe('bay leaves')
    expect(itemLabel('chicken thighs', 4, '')).toBe('chicken thighs')
    expect(itemLabel('cherry', 10, '')).toBe('cherries')
    expect(itemLabel('avocado', 2, '')).toBe('avocados')
    expect(itemLabel('fish', 2, '')).toBe('fish')
  })
  it('leaves items with a unit or no amount alone', () => {
    expect(itemLabel('egg', 2, 'cup')).toBe('egg')
    expect(itemLabel('salt', null, '')).toBe('salt')
  })
})

describe('unitLabel', () => {
  it('pluralizes units that take a plural', () => {
    expect(unitLabel('cup', 2)).toBe('cups')
    expect(unitLabel('cup', 0.5)).toBe('cup')
    expect(unitLabel('tbsp', 3)).toBe('tbsp')
    expect(unitLabel('', 3)).toBe('')
  })
})
