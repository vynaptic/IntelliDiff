// The navigation bar's groups, the link that is current and the browser tab's title.
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { currentOf, navOf, titleOf } from '../src/nav.ts';

const screens = (groups: { screen: string }[][]) => groups.map((group) => group.map((link) => link.screen));

test('a viewer in an open build sees the three groups everyone has', () => {
  const nav = navOf({ sso: false, admin: false, notifications: false, licensed: false });
  assert.deepEqual(screens(nav), [['runs', 'specs', 'targets'], ['schedules', 'prechecks'], ['readiness']]);
});

test('single sign-on adds approvals, an admin the admin screen, and settings the notifications and the licence', () => {
  const nav = navOf({ sso: true, admin: true, notifications: true, licensed: true });
  assert.deepEqual(screens(nav), [
    ['runs', 'specs', 'targets'],
    ['schedules', 'prechecks'],
    ['readiness', 'approvals'],
    ['admin', 'notifications', 'licence'],
  ]);
});

test('the current link is the screen of the address, whatever is opened inside it', () => {
  assert.equal(currentOf(['runs', 'R-1', 'keys', 'K-1']), 'runs');
  assert.equal(currentOf(['admin', 'audit']), 'admin');
  assert.equal(currentOf(['readiness', 'pair', 'brief']), 'readiness');
});

test('an empty or unknown address is the runs list, as the router shows it', () => {
  assert.equal(currentOf([]), 'runs');
  assert.equal(currentOf(['nowhere']), 'runs');
  assert.equal(titleOf(['nowhere']), 'Runs - DeltaSleuth');
});

test('a list screen is titled by its name', () => {
  assert.equal(titleOf([]), 'Runs - DeltaSleuth');
  assert.equal(titleOf(['prechecks']), 'Table pre-checks - DeltaSleuth');
  assert.equal(titleOf(['admin', 'audit']), 'Admin - DeltaSleuth');
  assert.equal(titleOf(['signin']), 'Sign in - DeltaSleuth');
});

test('a screen of one thing is titled by that thing', () => {
  assert.equal(titleOf(['runs', 'R-1']), 'Run R-1 - DeltaSleuth');
  assert.equal(titleOf(['runs', 'R-1', 'keys', 'K-1']), 'Key K-1 - DeltaSleuth');
  assert.equal(titleOf(['specs', 'new']), 'New comparison - DeltaSleuth');
  assert.equal(titleOf(['specs', '7']), 'Spec #7 - DeltaSleuth');
  assert.equal(titleOf(['schedules', 'S-1']), 'Schedule S-1 - DeltaSleuth');
  assert.equal(titleOf(['prechecks', 'P-1', 'pairs', '2']), 'Table pre-check P-1 - DeltaSleuth');
  assert.equal(titleOf(['readiness', 'orders']), 'Readiness of orders - DeltaSleuth');
  assert.equal(titleOf(['readiness', 'orders', 'brief']), 'Cut-over brief of orders - DeltaSleuth');
  assert.equal(titleOf(['approvals', 'A-1']), 'Approval A-1 - DeltaSleuth');
});
