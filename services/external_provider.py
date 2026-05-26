import httpx
import logging
import asyncio
import math

logger = logging.getLogger(__name__)

class ExternalProvider:
    def __init__(self, name, url, api_key, profit_margin, min_profit=0.0, server_type="standard", extra_id=None):
        self.name = name
        self.url = url.rstrip('/') + '/'
        self.api_key = api_key
        self.profit_margin = profit_margin
        self.min_profit = min_profit
        self.server_type = server_type
        self.extra_id = extra_id

    def get_base_params(self, action):
        """Prepare base parameters for API calls."""
        if self.server_type == "lion":
            params = {
                "action": action,
                "apiKey": self.api_key,
                "YourID": self.extra_id
            }
        else:
            # Standard (Spider/Max-TG)
            params = {
                "action": action,
                "apiKey": self.api_key,
                "apiKay": self.api_key  # Some panels have this typo
            }
        return params

    async def get_countries(self):
        """Fetch available countries and prices from the provider."""
        try:
            async with httpx.AsyncClient() as client:
                action = "country_info" if self.server_type == "lion" else "getCountrys"
                params = self.get_base_params(action)
                
                logger.info(f"Fetching countries from {self.name}: {self.url} with {params}")
                resp = await client.get(self.url, params=params, timeout=15.0)
                if resp.status_code == 200:
                    data = resp.json()
                    logger.info(f"Response from {self.name}: {data}")
                    return data
                else:
                    logger.warning(f"Failed to fetch countries from {self.name}: {resp.status_code}")
                    return []
        except Exception as e:
            logger.error(f"Error fetching countries from {self.name}: {e}")
            return []

    async def get_balance(self):
        """Fetch the current balance from the provider."""
        try:
            async with httpx.AsyncClient() as client:
                # 1. Determine the correct action
                if self.server_type == "lion":
                    action = "get_balance" # TG-Lion specifically uses 'get_balance'
                else:
                    action = "getBalance"
                    
                params = self.get_base_params(action)
                
                logger.info(f"Fetching balance from {self.name}: {self.url} (Action: {action})")
                resp = await client.get(self.url, params=params, timeout=15.0)
                
                if resp.status_code == 200:
                    text = resp.text.strip()
                    logger.info(f"Raw balance response from {self.name}: {text}")
                    
                    # 1. Try parsing as JSON
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            # Common field names for balance
                            balance_keys = ["wallet", "balance", "Balance", "money", "credit", "amount", "balans", "sum", "user_balance", "available_balance", "credits"]
                            
                            def parse_numeric(v):
                                """Helper to extract float from strings like '1.4 USD' or '$5.00'"""
                                if v is None: return None
                                if isinstance(v, (int, float)): return float(v)
                                try:
                                    # Try direct conversion
                                    return float(v)
                                except:
                                    # Try cleaning the string (remove currency, take first part)
                                    try:
                                        # Remove common currency symbols and units
                                        cleaned = str(v).lower().replace('usd', '').replace('$', '').replace('€', '').replace('₽', '').strip()
                                        # Take the first word (in case of '1.4 USD')
                                        first_part = cleaned.split()[0]
                                        return float(first_part)
                                    except:
                                        return None

                            # A. Direct search
                            for key in balance_keys:
                                if key in data and data[key] is not None:
                                    val = parse_numeric(data[key])
                                    if val is not None:
                                        return {"status": "success", "balance": val}
                            
                            # B. Nested search (Common for Standard panels like Max-TG/Spider)
                            for sub in ['result', 'user', 'info', 'data']:
                                if sub in data:
                                    # If result is a dict
                                    if isinstance(data[sub], dict):
                                        for key in balance_keys:
                                            v = data[sub].get(key)
                                            if v is not None:
                                                val = parse_numeric(v)
                                                if val is not None:
                                                    return {"status": "success", "balance": val}
                                    # If result is a raw value (some panels)
                                    elif sub == 'result':
                                        val = parse_numeric(data[sub])
                                        if val is not None:
                                            return {"status": "success", "balance": val}

                            # C. Handle specific status/ok flags
                            msg = data.get("message") or data.get("msg") or data.get("error") or data.get("status")
                            if msg and msg not in ["success", "ok", True]:
                                keys_found = ", ".join(data.keys())
                                return {"status": "error", "message": f"{msg} (Keys: {keys_found})"}
                            
                            keys_found = ", ".join(data.keys())
                            return {"status": "error", "message": f"Balance missing. Keys: {keys_found}"}
                    except Exception as json_err:
                        logger.error(f"JSON Parse error in get_balance: {json_err}")
                    
                    # 2. Try parsing as raw number from text
                    try:
                        # Clean the text before parsing (remove 'USD' etc)
                        cleaned_text = text.lower().replace('usd', '').replace('$', '').strip()
                        first_word = cleaned_text.split()[0]
                        return {"status": "success", "balance": float(first_word)}
                    except:
                        pass
                            
                    return {"status": "error", "message": f"Format Error. Text: {text[:50]}"}
                else:
                    return {"status": "error", "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            logger.error(f"Error fetching balance from {self.name}: {e}")
            return {"status": "error", "message": str(e)}


    async def buy_number(self, country_code):
        """Order a new number from the provider."""
        try:
            async with httpx.AsyncClient() as client:
                params = self.get_base_params("getNumber")
                if self.server_type == "lion":
                    params["country_code"] = country_code
                else:
                    # Standard panels (Spider, Max-TG, SMS-Hub clones)
                    params["country"] = country_code
                    # Generic panels usually require a service code (ot is common for Telegram)
                    params["service"] = "ot"
                
                logger.info(f"Buying number from {self.name}: {self.url} with {params}")
                resp = await client.get(self.url, params=params, timeout=20.0)
                if resp.status_code == 200:
                    text = resp.text.strip()
                    logger.info(f"Buy response from {self.name}: {text}")
                    
                    # 1. Try parsing as JSON first
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            # Handle wrapped results (e.g., Spider/Max-TG uses result: {number: ..., id: ...})
                            res_node = data.get("result") if isinstance(data.get("result"), dict) else data
                            
                            if data.get("status") == "success" or data.get("ok") is True:
                                # Ensure number and id exist
                                res = {
                                    "status": "success",
                                    "number": res_node.get("number") or res_node.get("phone") or data.get("number"),
                                    "id": res_node.get("id") or res_node.get("id_activation") or res_node.get("hash_code") or data.get("id"),
                                    "hash_code": res_node.get("hash_code") or res_node.get("id") or res_node.get("id_activation") or data.get("hash_code")
                                }
                                if res["number"] and res["id"]:
                                    return res
                                else:
                                    logger.warning(f"API success but missing fields in {res_node}")
                    except Exception as json_err:
                        logger.debug(f"JSON Parse skipped in buy_number: {json_err}")
                    
                    # 2. Fallback to Raw Text parsing (SMS-Hub Style: ACCESS_NUMBER:ID:NUMBER)
                    if "ACCESS_NUMBER" in text:
                        parts = text.split(':')
                        if len(parts) >= 3:
                            return {
                                "status": "success",
                                "id": parts[1],
                                "hash_code": parts[1],
                                "number": parts[2]
                            }
                    
                    # 3. Handle common text errors
                    msg_lower = text.lower()
                    if any(err in msg_lower for err in ["no_numbers", "no_number", "out_of_stock", "no_numbers_available"]):
                        return {"status": "error", "message": "No numbers available"}
                    if any(err in msg_lower for err in ["no_balance", "no_money", "not_enough_funds", "access_balance", "insufficient_funds", "insufficient"]):
                        return {"status": "error", "message": "No balance in API provider"}
                    if "bad_key" in msg_lower or "error_key" in msg_lower:
                        return {"status": "error", "message": "Invalid API Key"}
                        
                    return {"status": "error", "message": text}
                return {"status": "error", "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            logger.error(f"Error buying number from {self.name}: {e}")
            return {"status": "error", "message": str(e)}

    async def get_code(self, hash_code, number=None):
        """Fetch the activation code for a purchased number."""
        try:
            async with httpx.AsyncClient() as client:
                # Some panels use 'getStatus' instead of 'getCode'
                action = "getCode"
                params = self.get_base_params(action)
                
                if self.server_type == "lion":
                    params["number"] = number
                else:
                    # Try both hash_code and id for compatibility
                    params["hash_code"] = hash_code
                    params["id"] = hash_code
                
                logger.info(f"Fetching code from {self.name}: {self.url} with {params}")
                resp = await client.get(self.url, params=params, timeout=15.0)
                if resp.status_code == 200:
                    text = resp.text.strip()
                    logger.info(f"GetCode response from {self.name}: {text}")
                    
                    # 1. Try JSON
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            # Handle wrapped results
                            res_node = data.get("result") if isinstance(data.get("result"), dict) else data
                            
                            # Spider/Max-TG format
                            if (data.get("status") == "success" or data.get("ok") is True) and (res_node.get("code") or res_node.get("otp")):
                                return {"status": "success", "code": res_node.get("code") or res_node.get("otp")}
                            
                            # Other JSON formats
                            code = res_node.get("code") or res_node.get("otp") or res_node.get("sms") or data.get("code")
                            if code:
                                return {"status": "success", "code": code}
                    except Exception as json_err:
                        logger.debug(f"JSON Parse skipped in get_code: {json_err}")
                    
                    # 2. Raw Text (SMS-Hub Style: STATUS_OK:CODE or STATUS_WAIT_CODE)
                    if "STATUS_OK" in text:
                        parts = text.split(':')
                        if len(parts) >= 2:
                            return {"status": "success", "code": parts[1]}
                    
                    if "STATUS_WAIT" in text:
                        return {"status": "error", "message": "Code not arrived yet"}
                        
                    # 3. Fallback for Spider/Max if they return just the code (unlikely but safe)
                    if text and len(text) <= 10 and text.isdigit():
                        return {"status": "success", "code": text}

                    # If first action failed, try getStatus for standard panels
                    if action == "getCode" and self.server_type != "lion":
                        params["action"] = "getStatus"
                        resp2 = await client.get(self.url, params=params, timeout=15.0)
                        if resp2.status_code == 200:
                            t2 = resp2.text.strip()
                            if "STATUS_OK" in t2:
                                return {"status": "success", "code": t2.split(':')[1]}
                            if "STATUS_WAIT" in t2:
                                return {"status": "error", "message": "Code not arrived yet"}

                    return {"status": "error", "message": text}
                return {"status": "error", "message": f"HTTP {resp.status_code}"}
        except Exception as e:
            logger.error(f"Error getting code from {self.name}: {e}")
            return {"status": "error", "message": str(e)}

    def calculate_price(self, provider_price):
        """Calculate selling price based on profit margin and minimum profit."""
        cost = float(provider_price)
        # Only return base cost if both margin and minimum profit are 0
        if self.profit_margin <= 0 and self.min_profit <= 0:
            return cost
        
        # Calculate percentage profit
        percent_profit = cost * (self.profit_margin / 100.0)
        # Final profit is the maximum of percentage profit or minimum profit
        final_profit = max(percent_profit, self.min_profit)
        
        final_price = cost + final_profit
        # Round up to 2 decimal places (e.g., 0.231 -> 0.24)
        return math.ceil(final_price * 100) / 100.0
