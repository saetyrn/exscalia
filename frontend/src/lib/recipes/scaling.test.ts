import { describe, expect, it } from 'vitest'
import { formatAmount, pickDisplay, renderStep, scaleIngredient } from './scaling'
import type { Ingredient } from './types'

const ing = (over: Partial<Ingredient>): Ingredient => ({
  id: 'x', amount: 1, unit: '', item: 'thing', prep: '', scaling: 'linear', estimated: false, group: null, ...over,
})

describe('worked example: written for 2, scaled to 3 (factor 1.5)', () => {
  const f = 1.5
  it('200 g noodles → 300 g (linear)', () => {
    const s = scaleIngredient(ing({ amount: 200, unit: 'g' }), f)
    expect(s.text).toBe('300 g')
    expect(s.markers).toEqual([])
  })
  it('4 tsp soy sauce → 2 tbsp (unit switch)', () => {
    expect(scaleIngredient(ing({ amount: 4, unit: 'tsp' }), f).text).toBe('2 tbsp')
  })
  it('⅔ cup stock → 1 cup (linear)', () => {
    expect(scaleIngredient(ing({ amount: 2 / 3, unit: 'cup' }), f).text).toBe('1 cup')
  })
  it('1 egg → 2 eggs, marked (whole numbers only)', () => {
    const s = scaleIngredient(ing({ amount: 1, scaling: 'whole' }), f)
    expect(s.text).toBe('2')
    expect(s.markers).toEqual(['rounded'])
  })
  it('½ tsp salt → ¾ tsp, marked (to taste)', () => {
    const s = scaleIngredient(ing({ amount: 0.5, unit: 'tsp', scaling: 'to_taste' }), f)
    expect(s.text).toBe('¾ tsp')
    expect(s.markers).toEqual(['to_taste'])
  })
})

describe('unit switching', () => {
  it('keeps the stored unit at factor 1', () => {
    expect(scaleIngredient(ing({ amount: 4, unit: 'tbsp' }), 1).text).toBe('4 tbsp')
    expect(scaleIngredient(ing({ amount: 6, unit: 'tsp' }), 1).text).toBe('6 tsp')
  })
  it('does not switch to awkward thirds of a tablespoon', () => {
    expect(pickDisplay(4, 'tsp')).toEqual({ unit: 'tsp', amount: 4 })
  })
  it('switches up to whole or half tablespoons', () => {
    expect(pickDisplay(4.5, 'tsp')).toEqual({ unit: 'tbsp', amount: 1.5 })
  })
  it('whole tablespoons beat a quarter cup; half a cup and up wins', () => {
    expect(pickDisplay(4, 'tbsp')).toEqual({ unit: 'tbsp', amount: 4 })
    expect(pickDisplay(8, 'tbsp')).toEqual({ unit: 'cup', amount: 0.5 })
    expect(pickDisplay(12, 'tbsp')).toEqual({ unit: 'cup', amount: 0.75 })
    expect(formatAmount(pickDisplay(16 / 3, 'tbsp').amount, 'cup')).toBe('⅓')
  })
  it('switches down below the unit minimum', () => {
    expect(scaleIngredient(ing({ amount: 2, unit: 'tbsp' }), 0.25).text).toBe('1½ tsp')
    expect(scaleIngredient(ing({ amount: 0.25, unit: 'cup' }), 0.5).text).toBe('2 tbsp')
    expect(scaleIngredient(ing({ amount: 1, unit: 'cup' }), 0.5).text).toBe('½ cup')
  })
  it('weights stay whole grams and only go to kg when clean', () => {
    expect(scaleIngredient(ing({ amount: 500, unit: 'g' }), 3).text).toBe('1.5 kg')
    expect(scaleIngredient(ing({ amount: 500, unit: 'g' }), 2.5).text).toBe('1250 g')
    expect(scaleIngredient(ing({ amount: 1, unit: 'kg' }), 0.5).text).toBe('500 g')
  })
  it('metric volumes', () => {
    expect(scaleIngredient(ing({ amount: 250, unit: 'ml' }), 5).text).toBe('1.25 l')
    expect(scaleIngredient(ing({ amount: 250, unit: 'ml' }), 1.5).text).toBe('375 ml')
  })
  it('imperial weights', () => {
    expect(scaleIngredient(ing({ amount: 8, unit: 'oz' }), 2).text).toBe('1 lb')
    expect(scaleIngredient(ing({ amount: 1, unit: 'lb' }), 0.5).text).toBe('½ lb')
    expect(scaleIngredient(ing({ amount: 1, unit: 'lb' }), 0.125).text).toBe('2 oz')
  })
  it('falls back to the closest clean amount', () => {
    expect(pickDisplay(0.4, 'cup')).toEqual({ unit: 'tbsp', amount: 6.5 })
  })
})

describe('fractions and counts', () => {
  it('uses kitchen fractions and pluralizes', () => {
    expect(scaleIngredient(ing({ amount: 1, unit: 'cup' }), 1.5).text).toBe('1½ cups')
    expect(scaleIngredient(ing({ amount: 2, unit: 'clove' }), 1.5).text).toBe('3 cloves')
    expect(scaleIngredient(ing({ amount: 1, item: 'onion' }), 0.5).text).toBe('½')
  })
  it('an eighth teaspoon is allowed', () => {
    expect(scaleIngredient(ing({ amount: 0.25, unit: 'tsp' }), 0.5).text).toBe('⅛ tsp')
  })
  it('whole items never drop below one', () => {
    const s = scaleIngredient(ing({ amount: 1, scaling: 'whole' }), 0.25)
    expect(s.text).toBe('1')
    expect(s.markers).toEqual(['rounded'])
  })
  it('estimated amounts carry the marker at every factor', () => {
    expect(scaleIngredient(ing({ amount: 2, unit: 'tbsp', estimated: true }), 1).markers).toEqual(['estimated'])
  })
  it('to taste with no amount shows no number', () => {
    const s = scaleIngredient(ing({ amount: null, scaling: 'to_taste' }), 2)
    expect(s.text).toBe('')
    expect(s.markers).toEqual(['to_taste'])
  })
})

describe('step references', () => {
  const butter = ing({ id: 'butter', amount: 2, unit: 'tbsp', item: 'butter' })
  it('replaces {butter} with the scaled amount', () => {
    const segs = renderStep('Toss noodles with {butter}.', [butter], 2)
    expect(segs.map((s) => s.text)).toEqual(['Toss noodles with ', '4 tbsp butter', '.'])
    expect(segs[1].kind).toBe('ref')
  })
  it('resolves by item name and flags unknown references', () => {
    expect(renderStep('{Butter}', [butter], 1)[0].kind).toBe('ref')
    expect(renderStep('{cream}', [butter], 1)[0].kind).toBe('missing')
  })
  it('stays linear on braces that never close', () => {
    // An imported step is the page's text: from every "{" the reference
    // pattern rescanned to the end, so 50,000 of them took 1.7 s a render.
    const text = '{'.repeat(200_000) + '{butter}'
    const started = Date.now()
    const segs = renderStep(text, [butter], 1)
    expect(Date.now() - started).toBeLessThan(1000)
    expect(segs.map((s) => s.kind)).toEqual(['text', 'ref'])
    expect(segs[0].text.length).toBe(200_000)
  })
})
