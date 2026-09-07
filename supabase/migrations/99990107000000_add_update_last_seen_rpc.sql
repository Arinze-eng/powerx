CREATE OR REPLACE FUNCTION public.update_last_seen(p_user uuid)
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $$
  UPDATE public.profiles
     SET last_seen_at = now()
   WHERE id = p_user;
$$;
