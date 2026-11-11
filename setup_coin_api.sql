-- ==============================================================================
-- Run this ONCE in your Supabase SQL Editor:
-- https://supabase.com/dashboard/project/xvkjvuxhawlljakfkllu/sql
--
-- This creates a permanent, secure RPC function that allows the Python automation
-- script to assign custom coin amounts directly using the public anon key!
-- ==============================================================================

create or replace function public.hbu_set_coins(p_username text, p_coins integer)
returns jsonb language plpgsql security definer set search_path = public as $$
declare
  total integer;
begin
  update public.accounts
     set coins = greatest(0, least(p_coins, 2000000000))
   where lower(username) = lower(p_username)
   returning coins into total;

  if total is null then
    raise exception 'Account "%" not found.', p_username;
  end if;

  return jsonb_build_object('ok', true, 'username', p_username, 'coins', total);
end;
$$;

grant execute on function public.hbu_set_coins(text, integer) to anon, authenticated;
notify pgrst, 'reload schema';
