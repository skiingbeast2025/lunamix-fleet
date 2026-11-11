#!/usr/bin/env bash
# ==============================================================================
# Lunamix HBU2 Simulator One-Click Stop Script
# Run this script or double-click it anytime to stop all running simulator bots.
# ==============================================================================

echo "Stopping all Lunamix simulation processes..."

# 1. Attempt graceful SIGINT stop first
pkill -INT -f "simulate_hbu_chat.py" 2>/dev/null
pkill -INT -f "give_coins.py" 2>/dev/null
pkill -INT -f "custom_account_coins.py" 2>/dev/null
pkill -INT -f "custom_coins.py" 2>/dev/null
pkill -INT -f "gui_controller.py" 2>/dev/null

sleep 0.4

# 2. Force terminate if any are still lingering
pkill -9 -f "simulate_hbu_chat.py" 2>/dev/null
pkill -9 -f "give_coins.py" 2>/dev/null
pkill -9 -f "custom_account_coins.py" 2>/dev/null
pkill -9 -f "custom_coins.py" 2>/dev/null
pkill -9 -f "gui_controller.py" 2>/dev/null

echo "✓ All Lunamix simulation processes stopped successfully."
